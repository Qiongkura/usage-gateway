"""Offline coverage for entry failures and accounting ownership."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request
from starlette.responses import StreamingResponse
from fastapi.testclient import TestClient

from app import main
from app.db import Store
from app.models import UsageRecord


class EntryFailures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "usage.db")
        self.app = SimpleNamespace(state=SimpleNamespace(store=self.store))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def request(self):
        return Request({"type": "http", "method": "POST", "path": "/v1/messages",
                        "headers": [], "app": self.app})

    def test_entry_exception_records_sanitized_failure(self):
        def fail_auth(_request):
            raise RuntimeError("secret exception")

        with patch.object(main, "_auth", side_effect=fail_auth), \
             self.assertLogs("usage-gateway", level="ERROR") as logs:
            response = asyncio.run(main._handle(self.request(), "/messages"))
        self.assertEqual(response.status_code, 500)
        row = dict(self.store.conn.execute("SELECT * FROM usage_records").fetchone())
        trace = dict(self.store.conn.execute("SELECT * FROM request_traces").fetchone())
        self.assertEqual((row["result"], row["error_stage"], row["error_status"]),
                         ("internal_error", "entry", 500))
        self.assertEqual(trace["trace_id"], response.headers["x-trace-id"])
        self.assertNotIn("secret exception", str(row) + str(trace) + str(response.body))
        self.assertIn("RuntimeError", str(logs.output))
        self.assertIn("test_entry_internal_error.py", str(logs.output))
        self.assertNotIn("secret exception", str(logs.output))

    def test_delegated_failure_does_not_record_twice(self):
        async def fail_after_record(request, path):
            request.state.recording_attempted = True
            self.store.record_request(usage_record=UsageRecord(ts=1, source="gateway", raw={"request_id": "existing"}), ts=1)
            raise RuntimeError("sensitive-after-accounting")
        with patch.object(main, "_handle_entry", side_effect=fail_after_record), \
             self.assertLogs("usage-gateway", level="ERROR") as logs:
            response = asyncio.run(main._handle(self.request(), "/messages"))
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(b"sensitive-after-accounting", response.body)
        self.assertIn("RuntimeError", str(logs.output))
        self.assertIn("test_entry_internal_error.py", str(logs.output))
        self.assertNotIn("sensitive-after-accounting", str(logs.output))
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM usage_records").fetchone()[0], 1)

    def test_real_endpoint_proxy_failure_before_or_after_record(self):
        ck = SimpleNamespace(name="test", allow_models=[])
        upstream = SimpleNamespace()
        cfg = SimpleNamespace(find_key=lambda _: ck, route=lambda _: (upstream, "model", None),
                              find_by_prefix=lambda _: upstream, failover=False)
        async def fail(**kwargs):
            if write_first:
                self.store.record_request(usage_record=UsageRecord(ts=1, source="gateway", raw={"request_id": "saved"}), ts=1)
            raise RuntimeError("sensitive-proxy-error")
        with patch.object(main.app.state, "cfg", cfg, create=True), \
             patch.object(main.app.state, "store", self.store, create=True), \
             patch.object(main.app.state, "tracker", object(), create=True), \
             patch.object(main, "forward_with_failover", side_effect=fail), \
             self.assertLogs("usage-gateway", level="ERROR") as logs:
            client = TestClient(main.app, raise_server_exceptions=True)
            for write_first in (False, True):
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer test"}, json={"model": "p/model"})
                self.assertEqual(response.status_code, 500)
                self.assertNotIn("sensitive-proxy-error", response.text)
            self.assertNotIn("sensitive-proxy-error", str(logs.output))
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM usage_records").fetchone()[0], 1)

    def test_real_endpoint_rejection_survives_persistence_failure(self):
        cfg = SimpleNamespace(find_key=lambda _: None)
        with patch.object(main.app.state, "cfg", cfg, create=True), \
             patch.object(main.app.state, "store", self.store, create=True), \
             patch.object(self.store, "record_request", side_effect=RuntimeError("secret-store")), \
             self.assertLogs("usage-gateway", level="ERROR") as logs:
            response = TestClient(main.app, raise_server_exceptions=True).post(
                "/v1/messages", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(response.status_code, 401)
        self.assertIn("x-trace-id", response.headers)
        self.assertNotIn("secret-store", str(logs.output) + response.text)

    def test_stream_wrapper_closes_inner_iterator_with_session_context(self):
        session_context = main.session_context
        events = []
        async def inner():
            try:
                yield b"first"
                yield b"second"
            finally:
                events.append(session_context.get())
        async def forward(**kwargs):
            return StreamingResponse(inner())
        ck = SimpleNamespace(name="test", allow_models=[])
        upstream = SimpleNamespace()
        cfg = SimpleNamespace(find_key=lambda _: ck, route=lambda _: (upstream, "model", None),
                              find_by_prefix=lambda _: upstream, failover=False)
        async def run():
            request = self.request()
            request.scope["headers"] = [(b"authorization", b"Bearer test")]
            request.state.session_key = "fingerprint"
            request.state.session_source = "header"
            async def body():
                return b'{"model":"p/model"}'
            request.body = body
            self.app.state.cfg = cfg
            self.app.state.tracker = object()
            with patch.object(main, "forward_with_failover", side_effect=forward):
                response = await main._handle(request, "/messages")
                iterator = response.body_iterator
                self.assertEqual(await iterator.__anext__(), b"first")
                await iterator.aclose()
            self.assertEqual(events, [("fingerprint", "header")])
            self.assertEqual(session_context.get(), (None, None))
        asyncio.run(run())

    def test_database_failure_is_visible_without_sensitive_message(self):
        self.store.conn.execute("DROP TABLE request_traces")
        with self.assertLogs("usage-gateway.db", level="ERROR") as logs:
            self.store.record_request(usage_record=UsageRecord(ts=1, source="gateway", raw={"request_id": "secret"}),
                                      trace={"trace_id": "secret"}, ts=1)
        self.assertIn("OperationalError", str(logs.output))
        self.assertNotIn("secret", str(logs.output))
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM usage_records").fetchone()[0], 0)

    def test_fallback_write_failure_preserves_500(self):
        with patch.object(main, "_auth", side_effect=RuntimeError("secret")), \
             patch.object(self.store, "record_request", side_effect=RuntimeError("secret")), \
             self.assertLogs("usage-gateway", level="ERROR") as logs:
            response = asyncio.run(main._handle(self.request(), "/messages"))
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("secret", str(logs.output) + str(response.body))


if __name__ == "__main__":
    unittest.main()
