"""定时采集调度。"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from apscheduler.schedulers.background import BackgroundScheduler

from .alerts import QuotaAlerter, QuotaItem
from .collectors.base import Collector
from .collectors.vibe import VibeCollector
from .db import CollectorStateConflict, Store

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        store: Store,
        collectors: list[Collector],
        interval_sec: int = 60,
        alerter: QuotaAlerter | None = None,
        retention_days: int = 365,
        prices: Any = None,
        cfg: Any = None,
        probe_days: int = 7,
    ):
        self.store = store
        self.collectors = collectors
        self.interval = max(10, interval_sec)
        self.alerter = alerter
        # 用量记录保留天数（0 = 永久保留）
        self.retention_days = max(0, int(retention_days or 0))
        # 价格表：采集器入库的记录没有成本，这里按官方价补算
        self.prices = prices
        # 配置引用：自动探测模型后热应用可用性
        self.cfg = cfg
        # 模型可用性自动探测间隔（天，0 = 关闭）
        self.probe_days = max(0, int(getattr(cfg, "probe_days", probe_days) or 0))
        self._probe_lock = threading.Lock()
        self.sched = BackgroundScheduler(timezone="Asia/Shanghai")

    def start(self) -> None:
        # 首跑交给调度线程池（next_run_time=now），避免阻塞 uvicorn 启动数秒
        self.sched.add_job(
            self.tick, "interval", seconds=self.interval,
            id="collect", max_instances=1, coalesce=True,
            next_run_time=datetime.now(),
        )
        self.sched.start()

    @property
    def running(self) -> bool:
        return bool(self.sched.running)

    def shutdown(self) -> None:
        try:
            self.sched.shutdown(wait=False)
        except Exception:
            pass

    def tick(self) -> None:
        for c in self.collectors:
            try:
                self._run_usage(c)
            except Exception:
                log.exception("collector %s usage failed", c.name)
            try:
                self._run_quota(c)
            except Exception:
                log.exception("collector %s quota failed", c.name)

        # 额度告警：采集完就地判断，保证拿到的是刚写入的快照
        try:
            self._run_alerts()
        except Exception:
            log.exception("quota alert evaluation failed")

        # 每 10 分钟清理一次过期追踪（保留 7 天）+ 每天滚动备份数据库
        now = time.time()
        if not hasattr(self, "_last_prune"):
            self._last_prune = 0.0
        if now - self._last_prune > 600:
            self._last_prune = now
            try:
                deleted = self.store.prune_traces(keep_days=7)
                if deleted:
                    log.info("[prune] removed %d traces older than 7 days", deleted)
            except Exception:
                log.exception("prune traces failed")
            try:
                pruned = self.store.prune_quota_snapshots(keep_days=3)
                if pruned:
                    log.info("[prune] removed %d quota snapshots older than 3 days", pruned)
            except Exception:
                log.exception("prune quota snapshots failed")
            if self.retention_days > 0:
                try:
                    removed = self.store.prune_usage(keep_days=self.retention_days)
                    if removed:
                        log.info(
                            "[prune] removed %d usage records older than %d days",
                            removed, self.retention_days,
                        )
                except Exception:
                    log.exception("prune usage failed")
            try:
                backup = self.store.backup_database(
                    self.store.path.parent / "backups", keep=7
                )
                if backup is not None:
                    log.info("[backup] database snapshot -> %s", backup.name)
            except Exception:
                log.exception("database backup failed")

            # 每周自动探测模型可用性，结果热应用到运行中的网关（无需重启）
            if self.probe_days > 0 and self._probe_lock.acquire(blocking=False):
                try:
                    last = self.store.watermark("__model_probe__")
                    last_failure = self.store.watermark("__model_probe_failure__")
                    if now - last >= self.probe_days * 86400 and now - last_failure >= 300:
                        threading.Thread(target=self._weekly_probe, daemon=True, name="probe").start()
                    else:
                        self._probe_lock.release()
                except Exception:
                    self._probe_lock.release()
                    log.exception("probe scheduling failed")

    def _weekly_probe(self) -> None:
        try:
            from .probe_models import main as probe_main, probe_lock
            from .config import apply_model_health, load_config, load_model_health

            cfg = self.cfg if self.cfg is not None else load_config()
            with probe_lock(cfg) as acquired:
                if not acquired:
                    return
                now = time.time()
                if (now - self.store.watermark("__model_probe__") < self.probe_days * 86400
                        or now - self.store.watermark("__model_probe_failure__") < 300):
                    return
                log.info("[probe] weekly model availability probe started")
                try:
                    result = probe_main(locked=True, cfg=cfg)
                    health_file = Path(__file__).resolve().parent.parent / "model_health.json"
                    if self.cfg is not None and health_file.exists():
                        apply_model_health(self.cfg.upstreams, load_model_health(health_file))
                    if result != 0:
                        raise RuntimeError("probe incomplete")
                    self.store.set_watermark("__model_probe__", int(time.time()))
                except Exception:
                    self.store.set_watermark("__model_probe_failure__", int(time.time()))
                    log.exception("weekly probe failed")
        except Exception:
            log.exception("weekly probe scheduling failed")
        finally:
            self._probe_lock.release()

    def _run_usage(self, c: Collector) -> None:
        if isinstance(c, VibeCollector):
            for attempt in range(3):
                try:
                    self._run_usage_batch(c)
                    return
                except CollectorStateConflict:
                    if attempt == 2:
                        raise
            return
        self._run_usage_batch(c)

    def _run_usage_batch(self, c: Collector) -> None:
        if isinstance(c, VibeCollector):
            since, old_cursor = self.store.collector_position(c.name)
            records, cursor = c.prepare_usage(since, old_cursor)
        else:
            since = self.store.watermark(c.name)
            records = list(c.collect_usage(since))
        if not records:
            if isinstance(c, VibeCollector) and cursor != old_cursor:
                self.store.insert_usage(
                    [], collector_state=(c.name, since, cursor),
                    expected_collector_state=(since, old_cursor),
                )
            return
        # 采集器记录（本地客户端日志）没有成本，按价格表（官方价）补算；
        # 与网关路径的 record_from_usage 同一套口径
        if self.prices is not None:
            for r in records:
                if r.cost_usd is None:
                    r.cost_usd = self.prices.compute(
                        upstream=r.source, model=r.model,
                        input_tokens=r.input_tokens, output_tokens=r.output_tokens,
                        cache_read=r.cache_read, cache_write=r.cache_write,
                        at=datetime.fromtimestamp(r.ts, tz=timezone.utc),
                    )
        if isinstance(c, VibeCollector):
            inserted = self.store.insert_usage(
                records, collector_state=(c.name, max(r.ts for r in records), cursor),
                expected_collector_state=(since, old_cursor),
            )
        else:
            inserted = self.store.insert_usage(records)
            self.store.set_watermark(c.name, max(r.ts for r in records))
        if inserted:
            log.info("[%s] ingested %d new usage records", c.name, inserted)

    def _run_quota(self, c: Collector) -> None:
        snaps = list(c.collect_quota())
        if snaps:
            self.store.insert_quota(snaps)

    def _run_alerts(self) -> None:
        if self.alerter is None or not self.alerter.cfg.enabled:
            return
        items = load_quota_items(self.store, self.alerter.cfg.stale_sec)
        if items:
            self.alerter.evaluate(items)


def _window_span_hours(window: str) -> float | None:
    """从窗口名推断重置周期（小时），认不出来返回 None。

    例：'5h' / 'Claude and GPT models:5h' → 5，'weekly' → 168，'monthly' → 720。
    """
    w = (window or "").lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*h(?:\b|$)", w)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*d(?:\b|$)", w)
    if m:
        return float(m.group(1)) * 24
    if "week" in w:
        return 168.0
    if "month" in w:
        return 720.0
    if "day" in w:
        return 24.0
    return None


def compute_burn(conn, hours: int = 48, min_span_hours: float = 6.0,
                 stale_sec: int = 10800, now: int | None = None) -> dict[tuple[str, str, str], dict]:
    """按窗口独立估算线性消耗；样本不足、过期或跨重置时不外推。"""
    now = int(time.time()) if now is None else now
    rows = conn.execute(
        "SELECT source, COALESCE(account,'') AS a, COALESCE(window,'') AS w, "
        "ts, remaining_pct, resets_at FROM quota_snapshots "
        "WHERE ts > ? AND ts <= ? ORDER BY ts, id",
        (now - hours * 3600, now),
    ).fetchall()
    series: dict[tuple[str, str, str], list] = {}
    for row in rows:
        series.setdefault((row["source"], row["a"], row["w"]), []).append(row)

    out: dict[tuple[str, str, str], dict] = {}
    for key, pts in series.items():
        # 最新快照必须有真实百分比，不能沿用早于金额快照的旧百分比。
        if pts[-1]["remaining_pct"] is None or now - pts[-1]["ts"] > stale_sec:
            continue
        start = 0
        known_reset = pts[0]["resets_at"]
        if known_reset is not None and known_reset <= pts[0]["ts"]:
            known_reset = None
        for i in range(1, len(pts)):
            prev, cur = pts[i - 1], pts[i]
            # 重置时间变化、跨过已知重置时刻或明显回升都切断历史。
            # 回升可能是充值而非重置，但都不应混入同一消耗段。
            if (prev["remaining_pct"] is None or cur["remaining_pct"] is None
                    or (known_reset is not None and cur["resets_at"] is not None
                        and known_reset != cur["resets_at"])
                    or (prev["resets_at"] is not None and cur["resets_at"] is not None
                        and prev["resets_at"] != cur["resets_at"])
                    or any(reset is not None and prev["ts"] < reset <= cur["ts"]
                           for reset in (known_reset, prev["resets_at"], cur["resets_at"]))
                    or (prev["remaining_pct"] is not None and cur["remaining_pct"] is not None
                        and cur["remaining_pct"] - prev["remaining_pct"] > 0.2)):
                start = i
            # 缺失值不覆盖此前已知的未来重置，也不推测下一个周期。
            if cur["resets_at"] is not None:
                known_reset = cur["resets_at"]
            if known_reset is not None and known_reset <= cur["ts"]:
                known_reset = None
        seg = pts[start:]
        if len(seg) < 2 or seg[0]["remaining_pct"] is None:
            continue
        span_h = (seg[-1]["ts"] - seg[0]["ts"]) / 3600
        period_h = _window_span_hours(key[2])
        span_floor = 1.0 if period_h is not None and period_h <= 24 else min_span_hours
        if span_h < span_floor:
            continue
        drop = seg[0]["remaining_pct"] - seg[-1]["remaining_pct"]
        if drop <= 0.2 or not 0 <= seg[-1]["remaining_pct"] <= 100:
            continue
        rate = drop / span_h
        left = seg[-1]["remaining_pct"] / rate
        out[key] = {
            "per_hour": rate,
            "per_day": round(rate * 24, 3),
            "hours_left": round(min(left, 365.0 * 24), 1),
            "days_left": round(min(left / 24, 365.0), 1),
            "sample_span_hours": round(span_h, 2),
            "sample_count": len(seg),
        }
    return out


def load_quota_items(store: Store, stale_sec: int) -> list[QuotaItem]:
    """把最新快照折算成统一的百分比视图。

    - 有 remaining_pct 的（订阅额度）直接用
    - 只有 remaining_amt 的（余额）保留金额，不将历史最大值冒充额度上限
    - 超过 stale_sec 没更新的标记为 stale，不参与告警
    """
    conn = store.read_conn()
    now = int(time.time())

    rows = conn.execute(
        """
        SELECT source, account, window, remaining_pct,
               remaining_amt, currency, resets_at, ts
        FROM (
            SELECT q.*, ROW_NUMBER() OVER (
                PARTITION BY source, COALESCE(account,''), COALESCE(window,'')
                ORDER BY ts DESC, id DESC
            ) AS row_num
            FROM quota_snapshots q
        )
        WHERE row_num = 1
        ORDER BY source, account, window
        """
    ).fetchall()

    items: list[QuotaItem] = []
    burn = compute_burn(conn, stale_sec=stale_sec, now=now)
    for r in rows:
        pct = r["remaining_pct"]
        amt = r["remaining_amt"]

        items.append(QuotaItem(
            source=r["source"],
            account=r["account"] or "—",
            window=r["window"] or "—",
            remaining_pct=float(pct) if pct is not None else None,
            remaining_amt=amt,
            currency=r["currency"],
            resets_at=r["resets_at"],
            ts=int(r["ts"] or 0),
            stale=(now - int(r["ts"] or 0)) > stale_sec,
        ))
        b = burn.get((r["source"], r["account"] or "", r["window"] or ""))
        if b:
            q = items[-1]
            q.burn_per_hour = round(b["per_hour"], 4)
            q.burn_per_day = b["per_day"]
            q.sample_span_hours = b["sample_span_hours"]
            q.sample_count = b["sample_count"]
            q.prediction_note = ("近期样本线性外推，仅供参考；无重置时间" if q.resets_at is None
                                 else "近期样本线性外推，仅供参考")
            q.hours_left = b["hours_left"]
            q.days_left = b["days_left"]
            # 有重置时间就换算到「重置前预计剩多少」——直接拿百分比当天数用会误导：
            # 5h 窗口还剩 99% 不代表能用 97 天，重置前它只用掉零点几个点。
            if q.resets_at:
                hours_to_reset = (int(q.resets_at) - now) / 3600
                if hours_to_reset > 0:
                    q.hours_to_reset = round(hours_to_reset, 1)
                    projected = q.remaining_pct - b["per_hour"] * hours_to_reset
                    q.projected_pct = round(max(0.0, min(100.0, projected)), 1)
                    q.exhaust_before_reset = projected <= 0
    return items
