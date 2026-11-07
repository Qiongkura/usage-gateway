"""Mistral 多 key 优先级与降级测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import proxy as proxy_mod  # noqa: E402
from app.config import Upstream  # noqa: E402
from app.db import Store  # noqa: E402
from app.pricing import ModelPrice, PriceTable  # noqa: E402
from app.proxy import forward_with_failover  # noqa: E402

KEY_NEW = "key-new-aaaaaa111111"
KEY_OLD = "key-old-bbbbbb222222"


def make_upstream() -> Upstream:
    return Upstream(
        name="mistral",
        type="openai",
        base_url="http://fake.local/v1",
        api_key=KEY_NEW,
        api_keys=[KEY_NEW, KEY_OLD],
    )


class KeyFallbackTest(unittest.TestCase):
    def setUp(self):
        proxy_mod._KEY_COOLDOWN.clear()
        self.tmp = Path(tempfile.mkdtemp(prefix="ugkeys-"))
        self.store = Store(self.tmp / "usage.db")
        self.prices = PriceTable({
            "mistral": {"m": ModelPrice(input=2.0, output=8.0)},
        })
        self.cfg = SimpleNamespace(prices=self.prices, proxy_for=lambda up: None, failover=False)
        self.app = FastAPI()
        self.client = TestClient(self.app)
        self.seen_keys: list[str] = []

        @self.app.post("/v1/chat/completions")
        async def chat(request: Request):
            body = await request.body()
            return await forward_with_failover(
                request=request, cfg=self.cfg, store=self.store, tracker=None,
                path="/chat/completions", body=body,
                client_key_name="default", model="m",
                requested_model="mistral/m", candidates=[make_upstream()],
                allow_retry=False, max_rounds=1,
                parsed_payload=json.loads(body),
            )

    def tearDown(self):
        self.store.close()
        proxy_mod._HTTP_POOL.pop("", None)
        proxy_mod._KEY_COOLDOWN.clear()

    def _install_mock(self, fn):
        proxy_mod._HTTP_POOL[""] = httpx.AsyncClient(
            transport=httpx.MockTransport(fn), trust_env=False
        )

    def _post(self):
        return self.client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer gw-test"},
            json={"model": "mistral/m", "messages": [{"role": "user", "content": "hi"}]},
        )

    def test_429_on_primary_falls_back_to_second_key(self):
        def handler(request: httpx.Request) -> httpx.Response:
            auth = request.headers.get("authorization", "")
            self.seen_keys.append(auth.rsplit(" ", 1)[-1])
            if KEY_NEW in auth:
                return httpx.Response(429, json={"error": {"message": "quota exceeded, retry later"}})
            return httpx.Response(200, json={
                "id": "x", "model": "m",
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            })

        self._install_mock(handler)
        r = self._post()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["choices"][0]["message"]["content"], "ok")
        # 两个 key 按顺序都被用到
        self.assertEqual(self.seen_keys, [KEY_NEW, KEY_OLD])
        # 主 key 进入冷却，下一个请求直接走备用 key
        self.assertIn(f"mistral|{KEY_NEW[-6:]}", proxy_mod._KEY_COOLDOWN)
        # 用量照常落库
        row = self.store.read_conn().execute(
            "SELECT * FROM usage_records ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["input_tokens"], 100)
        self.assertEqual(row["output_tokens"], 10)

    def test_both_keys_429_returns_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.seen_keys.append(request.headers.get("authorization", "").rsplit(" ", 1)[-1])
            return httpx.Response(429, json={"error": {"message": "quota exceeded"}})

        self._install_mock(handler)
        r = self._post()
        self.assertEqual(r.status_code, 429)
        self.assertEqual(len(self.seen_keys), 2)   # 两个 key 都试过
        row = self.store.read_conn().execute(
            "SELECT * FROM usage_records ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], 429)


if __name__ == "__main__":
    unittest.main()
