"""流式转发端到端测试：mock 上游 + 真实落库，覆盖 _stream_response 的计量路径。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
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

MODEL = "fake-model"


def sse_frame(delta: dict, finish=None, usage=None) -> bytes:
    obj = {"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1,
           "model": MODEL, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        obj["usage"] = usage
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def make_upstream() -> Upstream:
    return Upstream(name="fake", type="openai", base_url="http://fake.local/v1", api_key="k")


class StreamProxyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ugstream-"))
        self.store = Store(self.tmp / "usage.db")
        self.prices = PriceTable({
            "fake": {MODEL: ModelPrice(input=2.0, output=8.0, cache_read=0.2)},
        })
        self.cfg = SimpleNamespace(
            prices=self.prices,
            proxy_for=lambda up: None,
            failover=False,
        )
        self.app = FastAPI()
        self.client = TestClient(self.app)

        @self.app.post("/v1/chat/completions")
        async def chat(request: Request):
            body = await request.body()
            return await forward_with_failover(
                request=request, cfg=self.cfg, store=self.store, tracker=None,
                path="/chat/completions", body=body,
                client_key_name="default", model=MODEL,
                requested_model=f"fake/{MODEL}",
                candidates=[make_upstream()],
                allow_retry=False, max_rounds=1,
                parsed_payload=json.loads(body),
            )

    def tearDown(self):
        self.store.close()
        proxy_mod._HTTP_POOL.pop("", None)

    def _install_mock(self, chunks: list[bytes], status: int = 200):
        handler = lambda request: httpx.Response(
            status, headers={"content-type": "text/event-stream"}, content=b"".join(chunks)
        )
        proxy_mod._HTTP_POOL[""] = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        )

    def _post(self, payload: dict):
        return self.client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer sk-gw-test"},
            json=payload,
        )

    def _last_record(self):
        row = self.store.read_conn().execute(
            "SELECT * FROM usage_records ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def test_stream_with_usage_recorded(self):
        """带 usage 的流式：token / 缓存 / 首字 / 成本全部落库，正文原样转发。"""
        chunks = [
            sse_frame({"role": "assistant", "content": ""}),
            sse_frame({"content": "你好"}),
            sse_frame({"content": "，世界"}),
            sse_frame({}, finish="stop", usage={
                "prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050,
                "prompt_tokens_details": {"cached_tokens": 800},
            }),
            b"data: [DONE]\n\n",
        ]
        self._install_mock(chunks)
        r = self._post({"model": "fake/fake-model", "stream": True,
                        "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers.get("x-trace-id"))
        # 正文按解析后的 delta 拼接断言（SSE 里 unicode 可能被转义）
        contents = []
        for line in r.text.splitlines():
            if line.startswith("data: ") and "[DONE]" not in line:
                d = json.loads(line[6:])["choices"][0]["delta"]
                if d.get("content"):
                    contents.append(d["content"])
        self.assertEqual("".join(contents), "你好，世界")

        rec = self._last_record()
        self.assertIsNotNone(rec)
        self.assertEqual(rec["source"], "fake")
        self.assertEqual(rec["model"], MODEL)
        self.assertEqual(rec["input_tokens"], 200)   # 1000 - 800
        self.assertEqual(rec["cache_read"], 800)
        self.assertEqual(rec["output_tokens"], 50)
        self.assertIsNotNone(rec["ttft_ms"])          # 真实首字（排除空预热帧）
        self.assertGreater(rec["latency_ms"], 0)
        # 成本：200×2/1M + 50×8/1M + 800×0.2/1M = 0.0004+0.0004+0.00016
        self.assertAlmostEqual(rec["cost_usd"], 0.00096, places=6)
        raw = json.loads(rec["raw_json"] or "{}")
        self.assertFalse(raw.get("estimated"))

    def test_stream_without_usage_estimated(self):
        """上游不给 usage：按启发式估算并打 estimated 标记。"""
        chunks = [
            sse_frame({"role": "assistant", "content": ""}),
            sse_frame({"content": "hello world"}),   # 11 字符 ≈ 3 tok
            b"data: [DONE]\n\n",
        ]
        self._install_mock(chunks)
        r = self._post({"model": "fake/fake-model", "stream": True,
                        "messages": [{"role": "user", "content": "tell me something"}]})
        self.assertEqual(r.status_code, 200)
        rec = self._last_record()
        self.assertIsNotNone(rec)
        self.assertGreater(rec["output_tokens"], 0)
        raw = json.loads(rec["raw_json"] or "{}")
        self.assertTrue(raw.get("estimated"))
        self.assertIsNotNone(rec["cost_usd"])        # 估算 token 也按价格表计价

    def test_upstream_error_recorded(self):
        """上游 500：错误原样透传，且失败请求落库（健康时间线可见）。"""
        self._install_mock([b'{"error": {"message": "boom"}}'], status=500)
        r = self._post({"model": "fake/fake-model", "stream": True,
                        "messages": [{"role": "user", "content": "hi"}]})
        # 500 且无重试：上游错误体原样透传
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.json()["error"]["message"], "boom")
        self.assertTrue(r.headers.get("x-trace-id"))

        rec = self._last_record()
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], 500)
        self.assertEqual(rec["client_key"], "default")

    def test_array_content_flattened_in_stream(self):
        """上游发数组型 content：转发给客户端前压平，同时 reasoning 分离。"""
        chunks = [
            sse_frame({"content": [
                {"type": "thinking", "thinking": [{"type": "text", "text": "想一想"}]},
                {"type": "text", "text": "答案"},
            ]}),
            sse_frame({}, finish="stop", usage={
                "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
            }),
            b"data: [DONE]\n\n",
        ]
        self._install_mock(chunks)
        r = self._post({"model": "fake/fake-model", "stream": True,
                        "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 200)
        # 客户端收到的 delta.content 必须是字符串（回归：Turn execution failed）
        deltas = [json.loads(l[6:]) for l in r.text.splitlines()
                  if l.startswith("data: ") and "[DONE]" not in l]
        contents = [d["choices"][0]["delta"].get("content") for d in deltas
                    if d.get("choices")]
        self.assertIn("答案", [c for c in contents if c])
        reasonings = [d["choices"][0]["delta"].get("reasoning_content") for d in deltas
                      if d.get("choices")]
        self.assertIn("想一想", [x for x in reasonings if x])
        # 思考 token 计入 usage.reasoning 吗？此 mock 无 reasoning_tokens 字段，验证不双计
        rec = self._last_record()
        self.assertEqual(rec["output_tokens"], 5)


if __name__ == "__main__":
    unittest.main()
