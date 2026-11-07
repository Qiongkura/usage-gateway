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
from .db import Store

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
        self.probe_days = max(0, int(probe_days or 0))
        self.sched = BackgroundScheduler(timezone="Asia/Shanghai")

    def start(self) -> None:
        # 首跑交给调度线程池（next_run_time=now），避免阻塞 uvicorn 启动数秒
        self.sched.add_job(
            self.tick, "interval", seconds=self.interval,
            id="collect", max_instances=1, coalesce=True,
            next_run_time=datetime.now(),
        )
        self.sched.start()

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
            if self.probe_days > 0 and now - getattr(self, "_last_probe", 0) > self.probe_days * 86400:
                self._last_probe = now
                threading.Thread(target=self._weekly_probe, daemon=True, name="probe").start()

    def _weekly_probe(self) -> None:
        try:
            from .probe_models import main as probe_main
            from .config import apply_model_health, load_model_health

            log.info("[probe] weekly model availability probe started")
            probe_main()
            health_file = Path(__file__).resolve().parent.parent / "model_health.json"
            if self.cfg is not None and health_file.exists():
                apply_model_health(self.cfg.upstreams, load_model_health(health_file))
                hidden = sum(len(u.unavailable_models) for u in self.cfg.upstreams)
                log.info("[probe] done, results applied (%d models hidden)", hidden)
        except Exception:
            log.exception("weekly probe failed")

    def _run_usage(self, c: Collector) -> None:
        since = self.store.watermark(c.name)
        records = list(c.collect_usage(since))
        if not records:
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


def compute_burn(conn, hours: int = 48, min_span_hours: float = 6.0) -> dict[tuple[str, str, str], dict]:
    """按最近 N 小时快照计算每个池子的燃烧率。

    返回 {(source, account, window): {"per_hour", "per_day", "hours_left", "days_left"}}，
    单位是「百分点/小时」。短周期窗口（如 5h）允许更短的样本跨度，否则它永远等不到 6 小时。
    数据不足（样本 < 2 或跨度不够）、未在消耗、或刚重置过的窗口不返回。
    """
    since = int(time.time()) - hours * 3600
    rows = conn.execute(
        "SELECT source, COALESCE(account,'') AS a, COALESCE(window,'') AS w, "
        "ts, remaining_pct FROM quota_snapshots "
        "WHERE ts > ? AND remaining_pct IS NOT NULL ORDER BY ts",
        (since,),
    ).fetchall()

    series: dict[tuple[str, str, str], list[tuple[int, float]]] = {}
    for r in rows:
        series.setdefault((r["source"], r["a"], r["w"]), []).append(
            (int(r["ts"]), float(r["remaining_pct"]))
        )

    out: dict[tuple[str, str, str], dict] = {}
    for k, pts in series.items():
        # 重置检测：百分比上跳超过 5 个点视为窗口重置，只取重置后的段
        start = 0
        for i in range(1, len(pts)):
            if pts[i][1] - pts[i - 1][1] > 5:
                start = i
        seg = pts[start:]
        if len(seg) < 2:
            continue
        span_h = (seg[-1][0] - seg[0][0]) / 3600
        # 5h 这类短窗口整个周期都不到 6 小时，按周期放宽样本跨度
        period_h = _window_span_hours(k[2])
        span_floor = 1.0 if period_h is not None and period_h <= 24 else min_span_hours
        if span_h < span_floor:
            continue
        drop = seg[0][1] - seg[-1][1]
        if drop <= 0.2:
            continue
        per_hour = drop / span_h
        hours_left = seg[-1][1] / per_hour
        out[k] = {
            "per_hour": round(per_hour, 4),
            "per_day": round(per_hour * 24, 3),
            "hours_left": round(min(hours_left, 365.0 * 24), 1),
            "days_left": round(min(hours_left / 24, 365.0), 1),
        }
    return out


def load_quota_items(store: Store, stale_sec: int) -> list[QuotaItem]:
    """把最新快照折算成统一的百分比视图。

    - 有 remaining_pct 的（订阅额度）直接用
    - 只有 remaining_amt 的（余额）用「剩余 / 初始」折算；
      没有初始值时按同窗口历史最大值当初始值
    - 超过 stale_sec 没更新的标记为 stale，不参与告警
    """
    conn = store.read_conn()
    now = int(time.time())

    rows = conn.execute(
        """
        SELECT q.source, q.account, q.window, q.remaining_pct,
               q.remaining_amt, q.currency, q.resets_at, q.ts
        FROM quota_snapshots q
        JOIN (
            SELECT source, COALESCE(account,'') AS acct,
                   COALESCE(window,'') AS win, MAX(ts) AS max_ts
            FROM quota_snapshots
            GROUP BY source, acct, win
        ) latest
          ON q.source = latest.source
         AND COALESCE(q.account,'') = latest.acct
         AND COALESCE(q.window,'')  = latest.win
         AND q.ts = latest.max_ts
        ORDER BY q.source, q.account, q.window
        """
    ).fetchall()

    items: list[QuotaItem] = []
    burn = compute_burn(conn)
    for r in rows:
        pct = r["remaining_pct"]
        amt = r["remaining_amt"]

        if pct is None and amt is not None:
            peak = conn.execute(
                """
                SELECT MAX(remaining_amt) AS peak FROM quota_snapshots
                WHERE source = ? AND COALESCE(account,'') = COALESCE(?,'')
                  AND COALESCE(window,'') = COALESCE(?,'')
                  AND remaining_amt IS NOT NULL
                """,
                (r["source"], r["account"], r["window"]),
            ).fetchone()
            base = (peak["peak"] if peak else None) or 0
            if base > 0:
                pct = max(0.0, min(100.0, float(amt) / float(base) * 100.0))

        if pct is None:
            continue

        items.append(QuotaItem(
            source=r["source"],
            account=r["account"] or "—",
            window=r["window"] or "—",
            remaining_pct=float(pct),
            remaining_amt=amt,
            currency=r["currency"],
            resets_at=r["resets_at"],
            ts=int(r["ts"] or 0),
            stale=(now - int(r["ts"] or 0)) > stale_sec,
        ))
        b = burn.get((r["source"], r["account"] or "", r["window"] or ""))
        if b:
            q = items[-1]
            q.burn_per_hour = b["per_hour"]
            q.burn_per_day = b["per_day"]
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
