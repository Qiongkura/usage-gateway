"""Offline regressions for historic trace data and session attribution."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import proxy
from app.api.stats import router
from app.config import Upstream
from app.db import Store
from app.main import _handle
from app.pricing import ModelPrice, PriceTable


# Frozen pre-attribution schema; do not derive this from the current application schema.
LEGACY_SCHEMA = """
CREATE TABLE usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT UNIQUE NOT NULL,
    ts INTEGER NOT NULL,
    source TEXT NOT NULL,
    account TEXT,
    model TEXT,
    requested_model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read INTEGER DEFAULT 0,
    cache_write INTEGER DEFAULT 0,
    reasoning INTEGER DEFAULT 0,
    cost_usd REAL,
    ttft_ms REAL,
    latency_ms REAL,
    status INTEGER DEFAULT 200,
    client_key TEXT,
    raw_json TEXT,
    ingested_at INTEGER NOT NULL
);
CREATE TABLE request_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT UNIQUE NOT NULL,
    ts INTEGER NOT NULL,
    source TEXT,
    model TEXT,
    requested_model TEXT,
    client_key TEXT,
    status INTEGER,
    total_ms REAL,
    spans_json TEXT
);
"""


class SecurityRegressionsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.store = Store(self.path / "usage.db")
        self.addCleanup(self.store.close)

    def make_api(self):
        app = FastAPI()
        app.state.store = self.store
        app.state.cfg = SimpleNamespace(admin_key="admin-secret")
        app.include_router(router)
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def insert_trace(self, trace_id, spans, ts=123):
        self.store.conn.execute(
            "INSERT INTO request_traces (trace_id, ts, source, model, client_key, status, total_ms, spans_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (trace_id, ts, "provider-private", "model-private", "client-private", 500, 12.5, spans),
        )
        self.store.conn.commit()

    def make_gateway(self):
        app = FastAPI()
        upstream = Upstream(name="fake", type="openai", base_url="http://fake.local/v1", api_key="k")
        ck = SimpleNamespace(name="client", allow_models=[])
        app.state.store = self.store
        app.state.tracker = None
        app.state.cfg = SimpleNamespace(find_key=lambda key: ck if key == "good" else None,
            route=lambda model: (upstream, "fake-model", None), find_by_prefix=lambda prefix: upstream,
            failover=False, retry_rounds=1, upstreams=[upstream],
            prices=PriceTable({"fake": {"fake-model": ModelPrice(input=2.0, output=8.0)}}),
            proxy_for=lambda up: None)

        @app.post("/entry")
        async def entry(request: Request):
            return await _handle(request, "/chat/completions")

        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                content=b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n\ndata: [DONE]\n\n')

        previous = proxy._HTTP_POOL.get("")
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        proxy._HTTP_POOL[""] = mock_client

        def restore_pool():
            if previous is None:
                proxy._HTTP_POOL.pop("", None)
            else:
                proxy._HTTP_POOL[""] = previous

        self.addCleanup(restore_pool)
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client, seen

    def test_historical_spans_are_allowlisted_in_list_and_detail(self):
        api = self.make_api()
        secret = "sensitive-prompt-token-url-marker"
        spans = [{"name": "upstream", "start_ms": 1, "duration_ms": 2.5,
                  "error": secret, "authorization": secret,
                  "detail": {"status": 500, "bytes": 20, "attempt": 1,
                             "tokens": {"input": 4, "output": 3, "secret": secret},
                             "url": secret, "body": secret}},
                 {"name": secret, "detail": {"status": secret, "bytes": -1}, "error": ""}]
        self.insert_trace("historic", json.dumps(spans))
        expected = [{"name": "upstream", "start_ms": 1, "duration_ms": 2.5,
                     "has_error": True, "detail": {"status": 500, "bytes": 20, "attempt": 1,
                                                   "tokens": {"input": 4, "output": 3}}},
                    {"has_error": False}]
        for path in ("/api/traces", "/api/traces/historic"):
            with self.subTest(path=path):
                response = api.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(secret, response.text)
                item = response.json()[0] if isinstance(response.json(), list) else response.json()
                self.assertNotIn("spans_json", item)
                self.assertEqual(item["spans"], expected)

    def test_invalid_historical_spans_do_not_break_trace_endpoints(self):
        api = self.make_api()
        cases = ["{broken", '"not a list"', "null", "[" * 2000]
        for index, raw in enumerate(cases):
            with self.subTest(case=index):
                trace_id = f"bad-{index}"
                self.insert_trace(trace_id, raw, ts=123 + index)
                listing = api.get("/api/traces")
                detail = api.get(f"/api/traces/{trace_id}")
                self.assertEqual(listing.status_code, 200)
                self.assertEqual(detail.status_code, 200)
                self.assertEqual(next(item for item in listing.json() if item["trace_id"] == trace_id)["spans"], [])
                self.assertEqual(detail.json()["spans"], [])

    def test_trace_detail_excludes_session_and_future_columns(self):
        api = self.make_api()
        self.insert_trace("private-fields", '[]')
        conn = self.store.conn
        conn.execute("ALTER TABLE request_traces ADD COLUMN future_private TEXT")
        conn.execute(
            "UPDATE request_traces SET session_key=?, session_source=?, future_private=? WHERE trace_id=?",
            ("private-session-key", "private-session-source", "private-future", "private-fields"),
        )
        conn.commit()
        for path in ("/api/traces", "/api/traces/private-fields"):
            with self.subTest(path=path):
                response = api.get(path)
                self.assertEqual(response.status_code, 200)
                item = response.json()[0] if isinstance(response.json(), list) else response.json()
                self.assertEqual(item["trace_id"], "private-fields")
                self.assertEqual(item["spans"], [])
                for field in ("session_key", "session_source", "future_private"):
                    self.assertNotIn(field, item)
                if path.endswith("private-fields"):
                    self.assertIsInstance(item["id"], int)
                self.assertNotIn("private-session", response.text)
                self.assertNotIn("private-future", response.text)

    def test_diagnostics_requires_admin_and_exports_only_allowlisted_data(self):
        api = self.make_api()
        secret = "private-error-prompt-credential-marker"
        self.insert_trace("private-trace-id", json.dumps([{"name": "route", "error": secret,
            "detail": {"status": 502, "body": secret}}]))
        for headers, params in [({}, {}), ({"x-admin-key": "wrong"}, {}), ({}, {"admin_key": "wrong"})]:
            with self.subTest(headers=headers, params=params):
                response = api.get("/api/diagnostics", headers=headers, params=params)
                self.assertIn(response.status_code, (401, 403))
                self.assertNotIn(secret, response.text)
        for kwargs in ({"headers": {"x-admin-key": "admin-secret"}},
                       {"params": {"admin_key": "admin-secret"}}):
            with self.subTest(authentication=next(iter(kwargs))):
                response = api.get("/api/diagnostics", **kwargs)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertIn("attachment", response.headers["content-disposition"])
                for forbidden in (secret, "private-trace-id", "provider-private", "model-private", "client-private", "admin-secret", "spans_json"):
                    self.assertNotIn(forbidden, response.text)
                self.assertEqual(response.json(), {"schema_version": 1, "traces": [{"ts": 123, "status": 500,
                    "total_ms": 12.5, "spans": [{"name": "route", "has_error": True, "detail": {"status": 502}}]}]})

    def test_legacy_database_migration_is_additive_and_repeatable(self):
        path = self.path / "old.db"
        columns = {"result", "error_stage", "error_kind", "error_status", "session_key", "session_source"}
        tables = ("usage_records", "request_traces")
        with closing(sqlite3.connect(path)) as conn:
            conn.row_factory = sqlite3.Row
            conn.executescript(LEGACY_SCHEMA)
            conn.execute("INSERT INTO usage_records (dedupe_key, ts, source, model, input_tokens, raw_json, ingested_at) "
                         "VALUES ('old', 1, 'legacy', 'old-model', 42, '{\"old\":true}', 2)")
            conn.execute("INSERT INTO request_traces (trace_id, ts, source, status, spans_json) "
                         "VALUES ('old-trace', 3, 'legacy', 502, '[{\"name\":\"old\"}]')")
            conn.commit()
            original = {}
            for table in tables:
                existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                self.assertTrue(columns.isdisjoint(existing), f"{table} already has migration columns")
                original[table] = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
        for iteration in range(2):
            with self.subTest(iteration=iteration):
                migrated = Store(path)
                try:
                    for table in tables:
                        self.assertTrue(columns <= {row[1] for row in migrated.conn.execute(f"PRAGMA table_info({table})")})
                        rows = [dict(row) for row in migrated.conn.execute(f"SELECT * FROM {table}")]
                        self.assertEqual(len(rows), 1)
                        self.assertTrue(all(rows[0][column] is None for column in columns))
                        self.assertEqual({key: rows[0][key] for key in original[table][0]}, original[table][0])
                finally:
                    migrated.close()

    def test_duplicate_session_headers_are_rejected(self):
        client, seen = self.make_gateway()
        response = client.post("/entry", headers=[("authorization", "Bearer good"),
            ("X-Session-ID", "first-private"), ("x-session-id", "second-private")],
            json={"model": "fake/fake-model"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(seen)
        self.assertNotIn("first-private", response.text)
        self.assertNotIn("second-private", response.text)
        row = dict(self.store.conn.execute("SELECT * FROM usage_records ORDER BY id DESC LIMIT 1").fetchone())
        self.assertEqual(row["result"], "rejected")
        self.assertIsNone(row["session_key"])
        self.assertNotIn("first-private", str(row))
        self.assertNotIn("second-private", str(row))

    def test_stream_session_attribution_is_persisted_without_raw_header(self):
        client, seen = self.make_gateway()
        headers = {"authorization": "Bearer good", "X-Session-ID": "private-session-id"}
        for _ in range(2):
            response = client.post("/entry", headers=headers, json={"model": "fake/fake-model", "stream": True})
            self.assertEqual(response.status_code, 200)
            self.assertIn("[DONE]", response.text)
        self.assertEqual(len(seen), 2)
        self.assertTrue(all("x-session-id" not in request.headers for request in seen))
        usage = [dict(row) for row in self.store.conn.execute("SELECT * FROM usage_records ORDER BY id")]
        traces = [dict(row) for row in self.store.conn.execute("SELECT * FROM request_traces ORDER BY id")]
        self.assertEqual(len(usage), 2)
        self.assertEqual(len(traces), 2)
        key = usage[0]["session_key"]
        self.assertIsInstance(key, str)
        self.assertEqual(len(key), 64)
        self.assertNotEqual(key, "private-session-id")
        for row in usage + traces:
            self.assertEqual(row["session_key"], key)
            self.assertEqual(row["session_source"], "header")
            self.assertNotIn("private-session-id", str(row))


if __name__ == "__main__":
    unittest.main()
