"""调用明细 /api/logs 与单次费用分项拆解测试。"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api import stats as stats_api  # noqa: E402
from app.db import Store  # noqa: E402
from app.models import UsageRecord  # noqa: E402
from app.pricing import ModelPrice, PriceTable  # noqa: E402

MODEL = "deepseek-v4-1-flash-1017"


def make_prices() -> PriceTable:
    return PriceTable({
        "mistral": {
            MODEL: ModelPrice(input=2.0, output=8.0, cache_read=0.2, cache_write=2.5),
        },
    })


def make_app(tmp: Path):
    store = Store(tmp / "usage.db")
    app = FastAPI()
    app.include_router(stats_api.router)
    app.state.cfg = SimpleNamespace(
        upstreams=[
            SimpleNamespace(name="mistral", billing_mode="subscription",
                            monthly_fee=20.0, prefix="mistral"),
        ],
        prices=make_prices(),
    )
    app.state.store = store
    return app, store


def rec(**kw) -> UsageRecord:
    now = kw.pop("ts", int(time.time()))
    defaults = dict(
        ts=now, source="mistral", model=MODEL, requested_model=None,
        client_key="default", input_tokens=0, output_tokens=0,
        cache_read=0, cache_write=0, reasoning=0, cost_usd=None,
        ttft_ms=None, latency_ms=None, status=200,
        raw={"request_id": f"trace-{now}"},
    )
    defaults.update(kw)
    return UsageRecord(**defaults)


class BreakdownTest(unittest.TestCase):
    def test_breakdown_math(self):
        prices = make_prices()
        at = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
        br = prices.breakdown(
            "mistral", MODEL,
            input_tokens=1_000_000, output_tokens=500_000,
            cache_read=8_000_000, cache_write=0, at=at,
        )
        self.assertAlmostEqual(br["input_cost"], 2.0, places=6)
        self.assertAlmostEqual(br["output_cost"], 4.0, places=6)
        self.assertAlmostEqual(br["cache_read_cost"], 1.6, places=6)
        self.assertAlmostEqual(br["total_cost"], 7.6, places=6)
        # 缓存省下：8M × (2.0 - 0.2) / 1M
        self.assertAlmostEqual(br["cache_saved_usd"], 14.4, places=6)
        self.assertEqual(br["multiplier"], 1.0)
        self.assertEqual(br["units"]["cache_read"], 0.2)
        # compute() 与 breakdown 同口径
        self.assertAlmostEqual(
            prices.compute("mistral", MODEL, 1_000_000, 500_000, 8_000_000, 0, at=at),
            br["total_cost"], places=8,
        )

    def test_breakdown_multiplier_and_missing_price(self):
        prices = make_prices()
        prices.table["mistral"][MODEL].multipliers = [
            {"when": {"utc_hours": {"gte": "00:00", "lt": "23:59"}}, "factor": 2.0},
        ]
        at = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
        br = prices.breakdown("mistral", MODEL, 1_000, 0, 0, 0, at=at)
        self.assertEqual(br["multiplier"], 2.0)
        self.assertAlmostEqual(br["input_cost"], 0.004, places=8)
        self.assertIsNone(prices.breakdown("mistral", "no-such-model", 1, 1, at=at))
        self.assertIsNone(prices.breakdown("no-such-upstream", MODEL, 1, 1, at=at))

    def test_zero_multiplier_is_not_defaulted_to_one(self):
        prices = make_prices()
        prices.table['mistral'][MODEL].multipliers = [{'factor': 0}]
        at = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
        self.assertEqual(prices.compute('mistral', MODEL, 1_000_000, 0, at=at), 0)
        breakdown = prices.breakdown('mistral', MODEL, 0, 0, 1_000_000, at=at)
        self.assertEqual(breakdown['multiplier'], 0)
        self.assertEqual(breakdown['cache_saved_usd'], 0)


class LogsApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ugtest-"))
        self.app, self.store = make_app(self.tmp)
        self.client = TestClient(self.app)
        now = int(time.time())
        self.now = now
        self.store.insert_usage([
            # 网关实时调用：cost_usd 为空 -> 接口按当前价格表补算
            rec(ts=now, client_key="default", input_tokens=1000, output_tokens=500,
                cache_read=8000, reasoning=200, ttft_ms=600.0, latency_ms=2400.0,
                raw={"request_id": "trace-gw"}),
            # 网关失败调用：另一模型，已带成本
            rec(ts=now - 60, client_key="vibe", model="mistral-large-latest",
                input_tokens=100, output_tokens=10, cost_usd=0.01, latency_ms=300.0,
                status=500, raw={"request_id": "trace-err"}),
            # 本地采集器记录：无 client_key、无延迟
            rec(ts=now - 120, client_key=None, input_tokens=2000, output_tokens=100,
                cache_read=500, raw={"request_id": "trace-col"}),
        ])

    def tearDown(self):
        self.store.close()

    def test_logs_gateway_default_and_cost_backfill(self):
        r = self.client.get("/api/logs", params={"range": "1d", "origin": "gateway"}).json()
        self.assertEqual(r["total"], 2)
        self.assertEqual(r["pages"], 1)
        items = r["items"]
        self.assertEqual(items[0]["client_key"], "default")
        # cost 补算：1000×2/1M + 500×8/1M + 8000×0.2/1M = 0.0076
        self.assertAlmostEqual(items[0]["cost_usd"], 0.0076, places=8)
        self.assertEqual(items[0]["cost_source"], "computed")
        self.assertAlmostEqual(items[0]["breakdown"]["cache_saved_usd"], 0.0144, places=8)
        self.assertEqual(items[0]["trace_id"], "trace-gw")
        self.assertEqual(items[0]["billing_mode"], "subscription")
        self.assertEqual(items[0]["alias"], "mistral")
        # token 口径
        self.assertEqual(items[0]["prompt_tokens"], 9000)
        self.assertEqual(items[0]["total_tokens"], 9500)
        self.assertAlmostEqual(items[0]["cache_rate"], 88.9, places=1)
        self.assertAlmostEqual(items[0]["tok_per_sec"], 500 / 2.4, places=1)
        self.assertAlmostEqual(items[0]["gen_tok_per_sec"], 500 / 1.8, places=1)

    def test_logs_summary_and_cache_saved(self):
        r = self.client.get("/api/logs", params={"range": "1d", "origin": "gateway"}).json()
        s = r["summary"]
        self.assertEqual(s["requests"], 2)
        self.assertEqual(s["failed"], 1)
        # 0.0076（补算）+ 0.01（记录值）
        self.assertAlmostEqual(s["cost_usd"], 0.0176, places=8)
        self.assertAlmostEqual(s["cost_recorded_usd"], 0.01, places=8)
        self.assertAlmostEqual(s["cost_computed_usd"], 0.0076, places=8)
        self.assertAlmostEqual(s["cache_saved_usd"], 0.0144, places=8)
        self.assertEqual(s["unpriced_requests"], 0)
        self.assertEqual(s["cache_saved_priced_requests"], 1)
        self.assertEqual(s["cache_saved_unpriced_requests"], 0)

    def test_cache_savings_coverage_is_independent_of_recorded_cost(self):
        self.store.insert_usage([
            rec(ts=self.now - 2, source='other', cache_read=1000, cost_usd=0.5),
        ])
        params = {"range": "1d", "origin": "all"}
        summary = self.client.get('/api/logs', params=params).json()['summary']
        self.assertEqual(summary['unpriced_requests'], 0)
        self.assertEqual(summary['cache_saved_priced_requests'], 2)
        self.assertEqual(summary['cache_saved_unpriced_requests'], 1)
        self.assertAlmostEqual(summary['cache_saved_usd'], 0.0153)

        self.app.state.cfg.prices = None
        summary = self.client.get('/api/logs', params=params).json()['summary']
        self.assertEqual(summary['cache_saved_priced_requests'], 0)
        self.assertEqual(summary['cache_saved_unpriced_requests'], 3)
        empty = self.client.get('/api/logs', params={**params, 'source': 'absent'}).json()['summary']
        self.assertEqual(empty['cache_saved_priced_requests'], 0)
        self.assertEqual(empty['cache_saved_unpriced_requests'], 0)
        self.assertEqual(empty['cache_saved_usd'], 0)

    def test_cache_savings_respects_minute_multiplier_boundaries(self):
        hour = (self.now // 3600 - 1) * 3600
        hour_text = datetime.fromtimestamp(hour, tz=timezone.utc).strftime('%H')
        self.app.state.cfg.prices.table['mistral'][MODEL].multipliers = [
            {'when': {'utc_hours': {'gte': f'{hour_text}:30', 'lt': f'{hour_text}:45'}}, 'factor': 2.0}
        ]
        self.store.insert_usage([
            rec(ts=hour + minute * 60, cache_read=1_000_000, session_key='minute-test',
                raw={'request_id': f'minute-{minute}'})
            for minute in (29, 30, 44, 45)
        ])
        response = self.client.get('/api/logs', params={
            'range': '1d', 'origin': 'all', 'session_key': 'minute-test',
        })
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data['summary']['cache_saved_priced_requests'], 4)
        self.assertAlmostEqual(data['summary']['cache_saved_usd'], 10.8)
        self.assertAlmostEqual(data['summary']['cache_saved_usd'],
                               sum(item['breakdown']['cache_saved_usd'] for item in data['items']))
        self.app.state.cfg.prices.table['mistral'][MODEL].multipliers = [{'factor': 0}]
        free = self.client.get('/api/logs', params={
            'range': '1d', 'origin': 'all', 'session_key': 'minute-test',
        }).json()
        self.assertEqual(free['summary']['cache_saved_usd'], 0)
        self.assertEqual(free['summary']['backfilled_requests'], 4)
        self.assertEqual(free['summary']['cost_computed_usd'], 0)

    def test_logs_origin_filter(self):
        r = self.client.get("/api/logs", params={"range": "1d", "origin": "all"}).json()
        self.assertEqual(r["total"], 3)
        r = self.client.get("/api/logs", params={"range": "1d", "origin": "collector"}).json()
        self.assertEqual(r["total"], 1)
        self.assertIsNone(r["items"][0]["client_key"])
        # 采集器记录继承所属上游的计费模式（mistral 配置为订阅制）
        self.assertEqual(r["items"][0]["billing_mode"], "subscription")

    def test_logs_status_and_model_filters(self):
        r = self.client.get(
            "/api/logs", params={"range": "1d", "origin": "all", "status": "error"}
        ).json()
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["items"][0]["status"], 500)

        r = self.client.get(
            "/api/logs", params={"range": "1d", "origin": "all", "model": "deepseek"}
        ).json()
        self.assertEqual(r["total"], 2)
        r = self.client.get(
            "/api/logs", params={"range": "1d", "origin": "all", "model": "large"}
        ).json()
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["items"][0]["model"], "mistral-large-latest")

    def test_logs_pagination(self):
        r = self.client.get(
            "/api/logs", params={"range": "1d", "origin": "all", "page": 2, "page_size": 2}
        ).json()
        self.assertEqual(r["total"], 3)
        self.assertEqual(r["pages"], 2)
        self.assertEqual(len(r["items"]), 1)
        # 按时间倒序：最早的一条在最后一页
        self.assertEqual(r["items"][0]["client_key"], None)

    def test_speed_includes_new_fields(self):
        r = self.client.get("/api/speed", params={"limit": 10}).json()
        latest = r["latest"]
        self.assertIsNotNone(latest)
        self.assertIsInstance(latest["id"], int)
        self.assertIn("input_tokens", latest)
        self.assertIn("cache_read", latest)
        self.assertIn("cost_usd", latest)
        # 采集器记录没有延迟，不应出现在 speed 里
        self.assertTrue(all(x["latency_ms"] for x in r["history"]))

    def test_speed_distinguishes_records_with_the_same_timestamp(self):
        now = int(time.time())
        self.store.insert_usage([
            rec(ts=now, output_tokens=10, latency_ms=1000, raw={"request_id": "speed-a"}),
            rec(ts=now, output_tokens=20, latency_ms=1000, raw={"request_id": "speed-b"}),
        ])
        rows = self.client.get('/api/speed', params={'limit': 2}).json()['history']
        self.assertEqual([r['output_tokens'] for r in rows], [20, 10])
        self.assertEqual(rows[0]['ts'], rows[1]['ts'])
        self.assertNotEqual(rows[0]['id'], rows[1]['id'])


if __name__ == "__main__":
    unittest.main()
