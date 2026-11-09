"""流式转发端到端测试：mock 上游 + 真实落库，覆盖 _stream_response 的计量路径。"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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

    def test_entry_rejections_and_session_fingerprint(self):
        from app.main import _handle
        ck = SimpleNamespace(name="client", allow_models=[])
        up = make_upstream()
        up.unavailable_models = [MODEL]
        self.app.state.store = self.store
        self.app.state.cfg = SimpleNamespace(find_key=lambda key: ck if key == "good" else None,
            route=lambda model: (up, MODEL, None), find_by_prefix=lambda prefix: up,
            failover=False, retry_rounds=1, upstreams=[up], prices=self.prices, proxy_for=lambda up: None)
        self.app.state.tracker = None
        @self.app.post("/entry")
        async def entry(request: Request):
            return await _handle(request, "/chat/completions")
        r = self.client.post("/entry", json={})
        self.assertEqual(r.status_code, 401)
        r = self.client.post("/entry", headers={"authorization": "Bearer good", "x-session-id": "invalid session"}, json={})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/entry", headers={"authorization": "Bearer good", "x-session-id": "session-private"}, json={})
        self.assertEqual(r.status_code, 400)
        row = dict(self.store.conn.execute("SELECT * FROM usage_records ORDER BY id DESC LIMIT 1").fetchone())
        self.assertEqual(row["result"], "rejected")
        self.assertEqual(row["session_source"], "header")
        self.assertNotIn("session-private", str(row))
        seen = []
        def handler(request):
            seen.append(request.headers)
            return httpx.Response(200, json={"model": MODEL, "usage": {"prompt_tokens": 1}})
        proxy_mod._HTTP_POOL[""] = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        r = self.client.post("/entry", headers={"authorization": "Bearer good", "x-session-id": "session-private"}, json={"model": "fake/fake-model"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("x-session-id", seen[0])
        row2 = dict(self.store.conn.execute("SELECT * FROM usage_records ORDER BY id DESC LIMIT 1").fetchone())
        self.assertEqual(row["session_key"], row2["session_key"])

    def test_invalid_model_types_are_recorded_without_forwarding(self):
        from app.main import _handle
        ck = SimpleNamespace(name="client", allow_models=[])
        self.app.state.store = self.store
        self.app.state.cfg = SimpleNamespace(find_key=lambda key: ck, route=lambda model: self.fail("routed invalid model"))
        @self.app.post("/invalid-model")
        async def entry(request: Request):
            return await _handle(request, "/chat/completions")
        for value in (1, True, ["x"], {"x": 1}, "   "):
            response = self.client.post("/invalid-model", headers={"authorization": "Bearer good"}, json={"model": value})
            self.assertEqual(response.status_code, 400)
            row = self._last_record()
            self.assertEqual((row["result"], row["error_kind"]), ("rejected", "invalid_model"))
            self.assertTrue(response.headers.get("x-trace-id"))

    def test_failover_filters_missing_alternates_but_keeps_explicit(self):
        from app.main import _handle
        from fastapi.responses import Response
        ck = SimpleNamespace(name="client", allow_models=[])
        explicit = Upstream(name="explicit", type="openai", base_url="http://fake.local", api_key="k", alias="group")
        missing = Upstream(name="missing", type="openai", base_url="http://fake.local", api_key="k", alias="group")
        available = Upstream(name="available", type="openai", base_url="http://fake.local", api_key="k", alias="group")
        explicit.unavailable_models = [MODEL]
        missing.unavailable_models = [MODEL]
        self.app.state.store = self.store
        self.app.state.tracker = None
        cfg = SimpleNamespace(find_key=lambda key: ck, route=lambda model: (explicit, MODEL, None),
            find_by_prefix=lambda prefix: explicit, failover=True, retry_rounds=1,
            upstreams=[explicit, missing, available])
        self.app.state.cfg = cfg
        @self.app.post("/routing")
        async def entry(request: Request):
            return await _handle(request, "/chat/completions")
        with patch("app.main.forward_with_failover", new_callable=AsyncMock, return_value=Response(status_code=204)) as forward:
            response = self.client.post("/routing", headers={"authorization": "Bearer good"}, json={"model": "group/fake-model"})
            self.assertEqual(response.status_code, 204)
            self.assertEqual({u.name for u in forward.await_args.kwargs["candidates"]}, {"explicit", "available"})
            cfg.failover = False
            self.client.post("/routing", headers={"authorization": "Bearer good"}, json={"model": "group/fake-model"})
            self.assertEqual(forward.await_args.kwargs["candidates"], [explicit])
            cfg.failover = True
            missing._model_health_checked = {MODEL: 1.0}
            self.client.post("/routing", headers={"authorization": "Bearer good"}, json={"model": "group/fake-model"})
            self.assertIn(missing, forward.await_args.kwargs["candidates"])

    def test_zero_usage_success_preserves_actual_status(self):
        for stream, force_non_stream in ((False, False), (True, False), (True, True)):
            with self.subTest(stream=stream, force_non_stream=force_non_stream):
                upstream = make_upstream()
                upstream.force_non_stream = force_non_stream
                @self.app.post(f"/zero-{stream}-{force_non_stream}")
                async def zero(request: Request):
                    return await forward_with_failover(request=request, cfg=self.cfg, store=self.store,
                        tracker=None, path="/chat/completions", body=await request.body(),
                        client_key_name="default", model=MODEL, requested_model=MODEL,
                        candidates=[upstream], allow_retry=False)
                content = b'data: [DONE]\n\n' if stream and not force_non_stream else b'{"choices": []}'
                proxy_mod._HTTP_POOL[""] = httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(201, content=content)), trust_env=False)
                response = self.client.post(f"/zero-{stream}-{force_non_stream}", json={"stream": stream})
                self.assertEqual(response.status_code, 201)
                record = self._last_record()
                self.assertEqual((record["status"], record["result"], record["input_tokens"], record["output_tokens"]),
                                 (201, "success", 0, 0))
                trace = self.store.conn.execute("SELECT status FROM request_traces ORDER BY id DESC LIMIT 1").fetchone()
                self.assertEqual(trace["status"], 201)

    def test_sensitive_error_not_logged_or_stored(self):
        secret = "private-prompt-and-response-marker"
        handler = lambda request: httpx.Response(500, json={"error": {"message": secret}})
        proxy_mod._HTTP_POOL[""] = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        with self.assertLogs("usage-gateway.proxy", level="WARNING") as logs:
            response = self._post({"model": "fake/fake-model", "messages": [{"content": secret}]})
        self.assertEqual(response.json()["error"]["message"], secret)
        self.assertNotIn(secret, str(logs.output))
        self.assertNotIn(secret, str(dict(self.store.conn.execute("SELECT * FROM usage_records").fetchone())))
        self.assertNotIn(secret, str(dict(self.store.conn.execute("SELECT * FROM request_traces").fetchone())))

    def test_stream_interruption_recorded_without_exception_text(self):
        class Broken(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield sse_frame({"content": "partial"})
                raise httpx.ReadError("private-exception-marker")
        proxy_mod._HTTP_POOL[""] = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Broken())), trust_env=False)
        response = self._post({"model": "fake/fake-model", "stream": True})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("private-exception-marker", response.text)
        record = self._last_record()
        self.assertEqual(record["status"], 200)
        self.assertEqual(record["result"], "stream_interrupted")
        self.assertEqual(record["error_stage"], "stream")
        self.assertNotIn("private-exception-marker", str(record))

    def test_cancelled_stream_is_not_upstream_failure(self):
        from app.tracing import Trace
        class Waiting(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield sse_frame({"content": "partial"})
                await asyncio.Event().wait()
        async def run():
            response = httpx.Response(200, stream=Waiting())
            result = proxy_mod._stream_response(resp=response, request_id="cancel-test", trace=Trace("cancel-test"),
                store=self.store, cfg=self.cfg, upstream=make_upstream(), model=MODEL,
                requested_model=MODEL, client_key_name="default", ts=1,
                started=__import__("time").perf_counter(), path="/chat/completions", body=b"{}")
            iterator = result.body_iterator
            await anext(iterator)
            task = asyncio.create_task(anext(iterator))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(run())
        rec = self._last_record()
        self.assertEqual((rec["status"], rec["result"]), (200, "client_cancelled"))

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
