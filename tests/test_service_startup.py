"""Exercise startup and legacy migration without touching the running gateway."""
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

import httpx
import yaml

from tests.test_security_regressions import LEGACY_SCHEMA


class ServiceStartupTest(unittest.TestCase):
    def test_legacy_database_survives_two_service_starts(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(prefix="usage-gateway-startup-") as temporary:
            isolated = Path(temporary)
            for directory in ("app", "web"):
                shutil.copytree(root / directory, isolated / directory,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for filename in ("pricing.yaml", "zen_prices.json"):
                shutil.copy2(root / filename, isolated / filename)
            data = isolated / "data"
            data.mkdir()
            db = data / "usage.db"
            now = int(time.time())
            with closing(sqlite3.connect(db)) as conn:
                conn.executescript(LEGACY_SCHEMA)
                conn.execute(
                    "INSERT INTO usage_records (dedupe_key, ts, source, model, input_tokens, "
                    "output_tokens, status, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("legacy", now, "offline", "legacy-model", 42, 8, 200, now),
                )
                conn.commit()

            with closing(socket.socket()) as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            config = isolated / "test-config.yaml"
            stats_key = secrets.token_urlsafe(24)
            admin_key = secrets.token_urlsafe(24)
            config.write_text(yaml.safe_dump({
                "server": {"host": "127.0.0.1", "port": port,
                           "stats_key": stats_key, "admin_key": admin_key},
                "data_dir": str(data), "upstreams": [], "probe_days": 0,
                "retention_days": 0, "alerts": {"enabled": False},
                "local_sources": {"opencode": {"enabled": False}, "vibe": {"enabled": False}},
            }), encoding="utf-8")
            env = os.environ.copy()
            env["USAGE_GATEWAY_CONFIG"] = str(config)
            env["PYTHONPATH"] = str(isolated)
            base = f"http://127.0.0.1:{port}"

            for iteration in range(2):
                with self.subTest(iteration=iteration), tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as log:
                    process = subprocess.Popen([sys.executable, "-m", "app.main"], cwd=isolated,
                                               env=env, stdout=log, stderr=subprocess.STDOUT)
                    try:
                        with httpx.Client(base_url=base, trust_env=False, timeout=2) as client:
                            deadline = time.monotonic() + 20
                            while True:
                                if process.poll() is not None:
                                    log.seek(0)
                                    self.fail(f"service exited during startup: {log.read()}")
                                try:
                                    response = client.get("/api/health")
                                    if response.status_code == 200:
                                        break
                                except httpx.TransportError:
                                    pass
                                if time.monotonic() >= deadline:
                                    self.fail("service did not start within 20 seconds")
                                time.sleep(0.1)
                            self.assertTrue(response.json()["ok"])
                            ready = client.get("/api/ready")
                            self.assertEqual(ready.status_code, 200, ready.text)
                            self.assertTrue(ready.json()["ok"])
                            self.assertEqual(client.get("/api/overview-usage?range=1d").status_code, 401)
                            response = client.get("/api/overview-usage?range=1d",
                                                  headers={"x-stats-key": stats_key})
                            self.assertEqual(response.status_code, 200, response.text)
                            self.assertEqual(response.json()["summary"]["total_tokens"], 50)
                            self.assertEqual(response.json()["summary"]["total_requests"], 1)
                            self.assertEqual(client.post("/api/admin/shutdown").status_code, 401)
                            response = client.post("/api/admin/shutdown", headers={"x-admin-key": admin_key})
                            self.assertTrue(response.json()["server_aware"])
                            self.assertEqual(process.wait(timeout=15), 0)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=10)
                with closing(sqlite3.connect(db)) as conn:
                    for table in ("usage_records", "request_traces"):
                        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                        self.assertTrue({"result", "error_stage", "error_kind", "error_status",
                                         "session_key", "session_source"} <= columns)
                    self.assertEqual(conn.execute("SELECT input_tokens, output_tokens FROM usage_records").fetchall(),
                                     [(42, 8)])


if __name__ == "__main__":
    unittest.main()
