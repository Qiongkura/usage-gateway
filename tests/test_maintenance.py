"""维护类功能测试：用量保留清理、vibe 增量采集、管理端点鉴权。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api import stats as stats_api  # noqa: E402
from app.alerts import QuotaAlerter, QuotaItem  # noqa: E402
from app.collectors.vibe import VibeCollector  # noqa: E402
from app.config import QuotaAlertConfig, apply_model_health  # noqa: E402
from app.db import Store  # noqa: E402
from app.models import QuotaSnapshot, UsageRecord  # noqa: E402
from app.scheduler import load_quota_items  # noqa: E402


class PruneUsageTest(unittest.TestCase):
    def test_prune_keeps_recent(self):
        tmp = Path(tempfile.mkdtemp(prefix="ugprune-"))
        store = Store(tmp / "usage.db")
        now = int(time.time())
        store.insert_usage([
            UsageRecord(ts=now - 400 * 86400, source="mistral", model="m",
                        input_tokens=10, output_tokens=1),
            UsageRecord(ts=now - 10 * 86400, source="mistral", model="m",
                        input_tokens=20, output_tokens=2),
            UsageRecord(ts=now, source="mistral", model="m",
                        input_tokens=30, output_tokens=3),
        ])
        removed = store.prune_usage(keep_days=365)
        self.assertEqual(removed, 1)
        rows = store.read_conn().execute("SELECT input_tokens FROM usage_records").fetchall()
        self.assertEqual(sorted(r["input_tokens"] for r in rows), [20, 30])
        # 再清一次：无新数据可删
        self.assertEqual(store.prune_usage(keep_days=365), 0)
        store.close()


def _write_meta(home: Path, sid: str, prompt: int, cached: int, out: int, mtime: int) -> None:
    d = home / "logs" / "session" / sid
    d.mkdir(parents=True, exist_ok=True)
    meta = {"metadata": {
        "session_prompt_tokens": prompt,
        "session_cached_tokens": cached,
        "session_completion_tokens": out,
    }}
    p = d / "meta.json"
    p.write_text(json.dumps(meta), encoding="utf-8")
    os.utime(p, (mtime, mtime))


class VibeDeltaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ugvibe-"))
        self.home = self.tmp / "vibe"
        self.state = self.tmp / "vibe_state.json"

    def _collector(self) -> VibeCollector:
        return VibeCollector(self.home, state_file=self.state)

    def test_incremental_deltas_across_restart(self):
        c1 = self._collector()
        _write_meta(self.home, "s1", prompt=1000, cached=400, out=100, mtime=1000)
        recs = list(c1.collect_usage(since_ts=0))
        # 首次见到会话：整份累计入账（prompt 含 cached，输入要减掉）
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].input_tokens, 600)
        self.assertEqual(recs[0].cache_read, 400)
        self.assertEqual(recs[0].output_tokens, 100)

        # 会话继续：累计值变大，只入差值
        _write_meta(self.home, "s1", prompt=1300, cached=400, out=250, mtime=2000)
        recs = list(c1.collect_usage(since_ts=1000))
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].input_tokens, 300)
        self.assertEqual(recs[0].cache_read, 0)
        self.assertEqual(recs[0].output_tokens, 150)

        # 重启（新实例、同一个状态文件）：不会把累计值重复统计
        c2 = self._collector()
        recs = list(c2.collect_usage(since_ts=2000))
        self.assertEqual(recs, [])
        _write_meta(self.home, "s1", prompt=1500, cached=600, out=300, mtime=3000)
        recs = list(c2.collect_usage(since_ts=2000))
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].input_tokens, 0)
        self.assertEqual(recs[0].cache_read, 200)
        self.assertEqual(recs[0].output_tokens, 50)

        # 累计值没变：不产出
        recs = list(c2.collect_usage(since_ts=3000))
        self.assertEqual(recs, [])

    def test_no_session_dir(self):
        c = self._collector()
        self.assertEqual(list(c.collect_usage(since_ts=0)), [])


def make_app(admin_key: str = ""):
    tmp = Path(tempfile.mkdtemp(prefix="ugadmin-"))
    store = Store(tmp / "usage.db")
    app = FastAPI()
    app.include_router(stats_api.router)
    app.state.cfg = SimpleNamespace(upstreams=[], prices=None, admin_key=admin_key)
    app.state.store = store
    return app, store


class AdminAuthTest(unittest.TestCase):
    def test_admin_open_when_no_key(self):
        app, _ = make_app()
        client = TestClient(app)
        self.assertEqual(client.post("/api/admin/checkpoint").status_code, 200)

    def test_admin_requires_key_when_configured(self):
        app, _ = make_app(admin_key="secret-key")
        client = TestClient(app)
        r = client.post("/api/admin/checkpoint")
        self.assertEqual(r.status_code, 401)
        # 头或查询参数携带正确 key 才放行
        self.assertEqual(
            client.post("/api/admin/checkpoint", headers={"x-admin-key": "secret-key"}).status_code,
            200,
        )
        self.assertEqual(
            client.post("/api/admin/checkpoint?admin_key=secret-key").status_code,
            200,
        )


class ModelHealthTest(unittest.TestCase):
    def test_only_gone_and_not_found_hidden(self):
        """429 / 超时 / 5xx 是瞬时的，不能因此把模型锁死一周。"""
        up = SimpleNamespace(name="mistral", unavailable_models=set())
        health = {"mistral": {
            "ok-model": {"status": "ok"},
            "gone-model": {"status": "gone", "code": 410},
            "missing-model": {"status": "not_found", "code": 404},
            "rate-limited-model": {"status": "rate_limited", "code": 429},
            "slow-model": {"status": "timeout"},
            "flaky-model": {"status": "server_error", "code": 503},
            "bad-request-model": {"status": "error", "code": 400},
        }}
        apply_model_health([up], health)
        self.assertEqual(up.unavailable_models, {"gone-model", "missing-model"})


class BurnRateTest(unittest.TestCase):
    def test_burn_prediction(self):
        tmp = Path(tempfile.mkdtemp(prefix="ugburn-"))
        store = Store(tmp / "usage.db")
        now = int(time.time())
        # 24h 前 50%，现在 25%：每天掉 25 个点 → 约 1 天耗尽
        for h, pct in ((24 * 3600, 50.0), (12 * 3600, 37.5), (0, 25.0)):
            store.insert_quota([QuotaSnapshot(
                ts=now - h, source="mistral", account=None, window="api_budget",
                remaining_pct=pct,
            )])
        items = load_quota_items(store, stale_sec=10800)
        it = [x for x in items if x.window == "api_budget" and x.source == "mistral"][0]
        self.assertIsNotNone(it.days_left)
        self.assertAlmostEqual(it.days_left, 1.0, delta=0.2)
        self.assertAlmostEqual(it.burn_per_day, 25.0, delta=1.0)
        store.close()

    def test_burn_short_window_uses_reset_projection(self):
        """5h 窗口：样本跨度不到 6h 也要能算；剩 90% 但 3h 后重置，
        不能把它当成「还能用 18 小时/几十天」，要换算成重置前预计剩余。"""
        tmp = Path(tempfile.mkdtemp(prefix="ugburn3-"))
        store = Store(tmp / "usage.db")
        now = int(time.time())
        # 2 小时掉 10 个点 → 5 点/小时；还剩 90%，重置在 3 小时后
        for h, pct in ((2 * 3600, 100.0), (3600, 95.0), (0, 90.0)):
            store.insert_quota([QuotaSnapshot(
                ts=now - h, source="opencode", account="a", window="5h",
                remaining_pct=pct, resets_at=now + 3 * 3600,
            )])
        items = load_quota_items(store, stale_sec=10800)
        it = [x for x in items if x.window == "5h"][0]
        self.assertAlmostEqual(it.burn_per_hour, 5.0, delta=0.1)
        self.assertAlmostEqual(it.hours_left, 18.0, delta=0.5)
        self.assertFalse(it.exhaust_before_reset)
        self.assertAlmostEqual(it.projected_pct, 75.0, delta=1.0)
        self.assertAlmostEqual(it.hours_to_reset, 3.0, delta=0.1)
        # 原来的「天数」仍保留在 API 里，但不参与有重置时间的判断
        self.assertAlmostEqual(it.days_left, 0.75, delta=0.1)
        store.close()

    def test_burn_flags_exhaust_before_reset(self):
        tmp = Path(tempfile.mkdtemp(prefix="ugburn4-"))
        store = Store(tmp / "usage.db")
        now = int(time.time())
        # 2 小时掉 80 个点 → 40 点/小时；还剩 20%，重置在 3 小时后
        for h, pct in ((2 * 3600, 100.0), (3600, 60.0), (0, 20.0)):
            store.insert_quota([QuotaSnapshot(
                ts=now - h, source="opencode", account="a", window="5h",
                remaining_pct=pct, resets_at=now + 3 * 3600,
            )])
        items = load_quota_items(store, stale_sec=10800)
        it = [x for x in items if x.window == "5h"][0]
        self.assertTrue(it.exhaust_before_reset)
        self.assertAlmostEqual(it.hours_left, 0.5, delta=0.1)
        store.close()

    def test_burn_skipped_when_not_draining(self):
        tmp = Path(tempfile.mkdtemp(prefix="ugburn2-"))
        store = Store(tmp / "usage.db")
        now = int(time.time())
        # 一直在涨（刚重置过）：不预测
        for h, pct in ((24 * 3600, 50.0), (0, 80.0)):
            store.insert_quota([QuotaSnapshot(
                ts=now - h, source="mistral", account=None, window="api_budget",
                remaining_pct=pct,
            )])
        items = load_quota_items(store, stale_sec=10800)
        it = [x for x in items if x.window == "api_budget"][0]
        self.assertIsNone(it.days_left)
        store.close()

    def test_burn_warn_before_threshold(self):
        """还没到告警阈值，但按速率 3 天内耗尽 → 提前预警。"""
        cfg = QuotaAlertConfig(enabled=True, warn_pct=20, critical_pct=10)
        alerter = QuotaAlerter(cfg)
        item = QuotaItem(
            source="mistral", account="api", window="api_budget",
            remaining_pct=60.0, days_left=2.0,
            ts=int(time.time()), stale=False,
        )
        events = alerter.evaluate([item])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "warn")
        self.assertIn("天耗尽", events[0].message)
        # 冷却期内不重复提醒
        self.assertEqual(alerter.evaluate([item]), [])


if __name__ == "__main__":
    unittest.main()
