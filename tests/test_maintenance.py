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
from app.collectors.vibe import VibeCollector  # noqa: E402
from app.db import Store  # noqa: E402
from app.models import UsageRecord  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
