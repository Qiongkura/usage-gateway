import asyncio
import json
import time
import unittest
import tempfile
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.config import MODEL_HEALTH_TTL, Upstream, apply_model_health
from app.probe_models import merge_result, probe_one, probe_upstream, retry_after_seconds


class ProbeTests(unittest.TestCase):
    def test_confirmation_expiry_and_transient(self):
        now = time.time() - 10
        first = merge_result({}, {"status": "not_found"}, now)
        up = SimpleNamespace(name="test", unavailable_models=set())
        apply_model_health([up], {"test": {"m": first}})
        self.assertFalse(up.unavailable_models)
        interrupted = merge_result(first, {"status": "timeout"}, now + 1)
        self.assertEqual(merge_result(interrupted, {"status": "gone"}, now + 2)["missing_count"], 1)
        second = merge_result(first, {"status": "gone"}, now + 1)
        self.assertEqual(second["missing_count"], 2)
        apply_model_health([up], {"test": {"m": second}})
        self.assertEqual(up.unavailable_models, {"m"})
        self.assertEqual(merge_result(second, {"status": "rate_limited"}, now + 2), second)
        self.assertEqual(merge_result(second, {"status": "ok"}, now + 2)["status"], "ok")
        second["checked_at"] = now - 15 * 86400
        apply_model_health([up], {"test": {"m": second}})
        self.assertFalse(up.unavailable_models)
        apply_model_health([up], {"test": {"m": {"status": "gone"}}})
        self.assertFalse(up.unavailable_models)

    def test_model_list_expires_without_reapplying_health(self):
        up = Upstream(name="test", type="openai", base_url="https://example.invalid", api_key="",
                      discovered_models=["missing", "live"])
        health = {"test": {"missing": {"status": "not_found", "missing_count": 2, "checked_at": 1000}}}
        with patch("app.config.time.time", return_value=1000) as clock:
            apply_model_health([up], health)
            self.assertEqual(up.all_models, ["live"])
            clock.return_value = 1000 + MODEL_HEALTH_TTL
            self.assertEqual(up.all_models, ["live"])
            clock.return_value += 1
            self.assertEqual(up.all_models, ["missing", "live"])
            self.assertTrue(up.matches("missing"))
            # 刷新探针快照后再次隐藏，成功状态则恢复。
            health["test"]["missing"]["checked_at"] = clock.return_value
            apply_model_health([up], health)
            self.assertEqual(up.all_models, ["live"])
            apply_model_health([up], {"test": {"missing": {"status": "ok"}}})
            self.assertEqual(up.all_models, ["missing", "live"])

    def test_failed_startup_discovery_restores_models_from_health(self):
        now = time.time()
        up = Upstream(name="test", type="openai", base_url="https://example.invalid",
                      api_key="", discovered_models=[])
        health = {"test": {
            "live": {"status": "timeout", "checked_at": now},
            "gone": {"status": "gone", "missing_count": 2, "checked_at": now},
            "expired": {"status": "ok", "checked_at": now - MODEL_HEALTH_TTL - 1},
        }}
        apply_model_health([up], health)
        self.assertEqual(up.discovered_models, ["live", "gone"])
        self.assertEqual(up.unavailable_models, {"gone"})
        self.assertEqual(up.all_models, ["live"])

    def test_static_models_are_not_replaced_by_health_fallback(self):
        up = Upstream(name="test", type="openai", base_url="https://example.invalid",
                      api_key="", models=["configured"])
        with patch("app.config.time.time", return_value=1000):
            apply_model_health([up], {"test": {"configured": {"status": "ok", "checked_at": 1000}}})
        self.assertEqual(up.discovered_models, [])
        self.assertEqual(up.all_models, ["configured"])

    def test_retry_after_bounded(self):
        self.assertEqual(retry_after_seconds("9999"), 9999)
        self.assertEqual(retry_after_seconds("-2"), 0)
        self.assertEqual(retry_after_seconds("bad"), 0)
        from datetime import datetime, timezone
        with patch("app.probe_models.datetime") as clock:
            clock.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self.assertEqual(retry_after_seconds("Thu, 01 Jan 2026 00:05:00 GMT"), 300)

    def test_error_content_not_saved(self):
        async def run():
            async def handler(request):
                return httpx.Response(404, json={"error": {"message": "secret"}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                up = SimpleNamespace(base_url="https://example.invalid", api_key="secret")
                _, result = await probe_one(client, up, "m", asyncio.Semaphore(1))
                self.assertNotIn("secret", str(result))
        asyncio.run(run())

    def test_schedule_persists_and_does_not_overlap(self):
        from app.scheduler import Scheduler
        store = MagicMock()
        store.watermark.return_value = int(time.time())
        scheduler = Scheduler(store, [], probe_days=7)
        with patch("app.scheduler.threading.Thread") as thread:
            scheduler.tick()
            thread.assert_not_called()
        store.watermark.return_value = 0
        scheduler._last_prune = 0
        with patch("app.scheduler.threading.Thread") as thread:
            scheduler.tick()
            thread.assert_called_once()
            store.set_watermark.assert_not_called()
            scheduler._last_prune = 0
            scheduler.tick()
            thread.assert_called_once()
        scheduler._probe_lock.release()

    def test_failure_watermark_and_success(self):
        from app.scheduler import Scheduler
        store = MagicMock()
        store.watermark.return_value = 0
        scheduler = Scheduler(store, [], probe_days=7)
        with tempfile.TemporaryDirectory() as tmp, patch("app.probe_models.LOCK_FILE", Path(tmp) / "lock"):
            scheduler.cfg = SimpleNamespace(data_dir=Path(tmp) / "data", upstreams=[])
            for result, key in [(1, "__model_probe_failure__"), (0, "__model_probe__")]:
                store.reset_mock()
                scheduler._probe_lock.acquire()
                with patch("app.probe_models.main", return_value=result), \
                     patch("app.scheduler.time.time", return_value=1_000_000):
                    scheduler._weekly_probe()
                self.assertEqual(store.set_watermark.call_args.args, (key, 1_000_000))
                self.assertEqual(store.set_watermark.call_count, 1)

    def test_cross_process_lock(self):
        from app.probe_models import probe_lock
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "lock")
            cfg = SimpleNamespace(data_dir=Path(tmp) / "data")
            script = ("from pathlib import Path; import app.probe_models as p; "
                      "from types import SimpleNamespace; "
                      "p.LOCK_FILE=Path(%r); "
                      "cfg=SimpleNamespace(data_dir=Path(%r)); "
                      "exec('with p.probe_lock(cfg) as acquired:\\n print(int(acquired))')") % (path, str(cfg.data_dir))
            with patch("app.probe_models.LOCK_FILE", Path(path)), probe_lock(cfg) as acquired:
                self.assertTrue(acquired)
                result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), "0")
            result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "1")

    def test_shared_data_lock_blocks_other_installation_and_releases(self):
        import app.probe_models as probe
        script = r'''
import asyncio, json, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
sys.path.insert(0, sys.argv[1])
import app.probe_models as probe
from app.main import discover_models
root, data, action = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
up = SimpleNamespace(name="x", prefix="x", models=["a"], no_proxy=True)
cfg = SimpleNamespace(data_dir=data, upstreams=[up])
calls = []
async def generation(*args):
    calls.append("generation")
    return {"a": {"status": "ok"}}
async def discovery(*args):
    calls.append("discovery")
with patch.object(probe, "LOCK_FILE", root / "lock"), \
     patch.object(probe, "HEALTH_FILE", root / "health"), \
     patch.object(probe, "COOLDOWN_FILE", root / "legacy"), \
     patch.object(probe, "probe_upstream", new=AsyncMock(side_effect=generation)), \
     patch("app.main._discover_models_locked", new=AsyncMock(side_effect=discovery)):
    if action == "probe":
        probe.main(cfg=cfg)
    else:
        asyncio.run(discover_models(cfg))
print("RESULT=" + json.dumps(calls))
'''
        project = str(Path(__file__).resolve().parent.parent)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b, data = root / "a", root / "b", root / "data"
            a.mkdir()
            b.mkdir()
            cfg = SimpleNamespace(data_dir=data)
            def run(action):
                result = subprocess.run([sys.executable, "-c", script, project, str(b), str(data), action],
                                        cwd=b, capture_output=True, text=True, check=True, timeout=30)
                return json.loads(next(line[7:] for line in result.stdout.splitlines()
                                       if line.startswith("RESULT=")))
            with patch.object(probe, "LOCK_FILE", a / "lock"), probe.probe_lock(cfg) as acquired:
                self.assertTrue(acquired)
                self.assertEqual(run("probe"), [])
                self.assertEqual(run("discovery"), [])
            self.assertEqual(run("probe"), ["generation"])
            self.assertEqual(run("discovery"), ["discovery"])

    def test_unusable_lock_directory_fails_closed(self):
        import app.probe_models as probe
        from app.main import discover_models
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "not-a-directory"
            data.write_text("x")
            cfg = SimpleNamespace(data_dir=data, upstreams=[])
            with patch.object(probe, "LOCK_FILE", Path(tmp) / "lock"), \
                 patch.object(probe, "probe_upstream", new=AsyncMock()) as generation, \
                 patch("app.main._discover_models_locked", new=AsyncMock()) as discovery:
                self.assertEqual(probe.main(cfg=cfg), 1)
                asyncio.run(discover_models(cfg))
                generation.assert_not_awaited()
                discovery.assert_not_awaited()

    def test_cooldown_survives_process_restart_for_discovery_and_probe(self):
        script = r'''
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, sys.argv[1])
import httpx
import app.probe_models as probe
from app.main import discover_models

data, instance, action = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
up = SimpleNamespace(name="x", prefix="x", models=[], api_key="", no_proxy=True,
                     discovered_models=[], base_url="https://example.invalid")
cfg = SimpleNamespace(upstreams=[up], data_dir=data, proxy_for=lambda _: None)
requests = []
def handler(request):
    requests.append(request.url.path)
    if action == "discovery_limit":
        return httpx.Response(429, headers={"Retry-After": "300"})
    return httpx.Response(200, json={"data": [{"id": "a"}]})
real_client = httpx.AsyncClient
def client(**kwargs):
    return real_client(transport=httpx.MockTransport(handler), **kwargs)
async def generation(*args):
    requests.append("generation")
    return {"a": {"status": "rate_limited", "retry_after": 300}}
if action.startswith("probe"):
    up.models = ["a"]
now = 1300 if action == "discovery_expired" else 1000
with patch.object(probe, "LOCK_FILE", instance / "lock"), \
     patch.object(probe, "HEALTH_FILE", instance / "health.json"), \
     patch.object(probe, "COOLDOWN_FILE", instance / "legacy.json"), \
     patch.object(probe, "load_config", return_value=cfg), \
     patch.object(probe.httpx, "AsyncClient", side_effect=client), \
     patch.object(probe, "probe_upstream", new=AsyncMock(side_effect=generation)), \
     patch.object(probe.time, "time", return_value=now):
    if action.startswith("probe"):
        probe.main()
    else:
        asyncio.run(discover_models(cfg))
print("RESULT=" + json.dumps(requests))
'''
        project = str(Path(__file__).resolve().parent.parent)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance_a, instance_b = root / "instance-a", root / "instance-b"
            instance_a.mkdir()
            instance_b.mkdir()

            def run(data, instance, action):
                result = subprocess.run(
                    [sys.executable, "-c", script, project, str(data), str(instance), action],
                    cwd=instance, capture_output=True, text=True, check=True, timeout=30,
                )
                return json.loads(next(line[7:] for line in result.stdout.splitlines()
                                       if line.startswith("RESULT=")))

            data = root / "probe-first"
            self.assertEqual(run(data, instance_a, "probe_limit"), ["generation"])
            self.assertEqual(json.loads((data / "model_probe_cooldown.json").read_text())["x"], 1300)
            self.assertEqual(run(data, instance_b, "discovery_blocked"), [])
            self.assertEqual(run(data, instance_b, "discovery_expired"), ["/models"])

            data = root / "discovery-first"
            self.assertEqual(run(data, instance_a, "discovery_limit"), ["/models"])
            self.assertEqual(json.loads((data / "model_probe_cooldown.json").read_text())["x"], 1300)
            self.assertEqual(run(data, instance_b, "probe_blocked"), [])

    def test_cooldown_persists_across_runs(self):
        import app.probe_models as probe
        up = SimpleNamespace(name="x", prefix="x", models=["a", "b"], no_proxy=True)
        cfg = SimpleNamespace(upstreams=[up])
        async def limited(*args):
            return {"a": {"status": "rate_limited", "retry_after": 300}}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(probe, "HEALTH_FILE", Path(tmp) / "health.json"), \
             patch.object(probe, "COOLDOWN_FILE", Path(tmp) / "legacy.json"), \
             patch.object(probe, "cooldown_path", return_value=Path(tmp) / "cooldown.json"), \
             patch.object(probe, "LOCK_FILE", Path(tmp) / "lock"), \
             patch.object(probe, "load_config", return_value=cfg), \
             patch.object(probe, "probe_upstream", new=AsyncMock(side_effect=limited)) as call, \
             patch.object(probe.time, "time", return_value=1000) as clock:
            cfg.data_dir = Path(tmp) / "data"
            self.assertEqual(probe.main(), 1)
            self.assertEqual(json.loads(probe.cooldown_path(cfg).read_text())["x"], 1300)
            clock.return_value = 1299
            self.assertEqual(probe.main(), 1)
            self.assertEqual(call.await_count, 1)
            clock.return_value = 1300
            self.assertEqual(probe.main(), 1)
            self.assertEqual(call.await_count, 2)

    def test_discovery_rate_limit_cooldown_across_runs(self):
        import app.probe_models as probe
        up = SimpleNamespace(name="x", prefix="x", models=[], api_key="secret",
                             base_url="https://example.invalid")
        cfg = SimpleNamespace(upstreams=[up], proxy_for=lambda _: None)
        requests = []

        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(429, headers={"Retry-After": "300"})
            return httpx.Response(200, json={"data": [{"id": "a"}]})

        real_client = httpx.AsyncClient
        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(probe, "HEALTH_FILE", Path(tmp) / "health.json"), \
             patch.object(probe, "COOLDOWN_FILE", Path(tmp) / "legacy.json"), \
             patch.object(probe, "cooldown_path", return_value=Path(tmp) / "cooldown.json"), \
             patch.object(probe, "LOCK_FILE", Path(tmp) / "lock"), \
             patch.object(probe, "load_config", return_value=cfg), \
             patch.object(probe.httpx, "AsyncClient", side_effect=client), \
             patch.object(probe, "probe_upstream", new=AsyncMock(return_value={"a": {"status": "ok"}})) as generation, \
             patch.object(probe.time, "time", return_value=1000) as clock:
            cfg.data_dir = Path(tmp) / "data"
            self.assertEqual(probe.main(), 1)
            self.assertEqual(json.loads(probe.cooldown_path(cfg).read_text())["x"], 1300)
            generation.assert_not_awaited()
            clock.return_value = 1299
            self.assertEqual(probe.main(), 1)
            self.assertEqual(len(requests), 1)
            generation.assert_not_awaited()
            clock.return_value = 1300
            self.assertEqual(probe.main(), 0)
            self.assertEqual(len(requests), 2)
            generation.assert_awaited_once()
            self.assertTrue(all(r.url.path == "/models" for r in requests))

    def test_discovery_rate_limit_missing_retry_after(self):
        import app.probe_models as probe
        up = SimpleNamespace(name="x", prefix="x", models=[], api_key="",
                             base_url="https://example.invalid")
        cfg = SimpleNamespace(upstreams=[up], proxy_for=lambda _: None)
        real_client = httpx.AsyncClient
        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(lambda request: httpx.Response(429)), **kwargs)

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(probe, "HEALTH_FILE", Path(tmp) / "health.json"), \
             patch.object(probe, "COOLDOWN_FILE", Path(tmp) / "legacy.json"), \
             patch.object(probe, "cooldown_path", return_value=Path(tmp) / "cooldown.json"), \
             patch.object(probe, "LOCK_FILE", Path(tmp) / "lock"), \
             patch.object(probe, "load_config", return_value=cfg), \
             patch.object(probe.httpx, "AsyncClient", side_effect=client), \
             patch.object(probe, "probe_upstream", new=AsyncMock()) as generation, \
             patch.object(probe.time, "time", return_value=1000):
            cfg.data_dir = Path(tmp) / "data"
            self.assertEqual(probe.main(), 1)
            self.assertEqual(json.loads(probe.cooldown_path(cfg).read_text())["x"], 1001)
            generation.assert_not_awaited()

    def test_startup_and_probe_share_cooldown_and_fail_closed(self):
        import app.probe_models as probe
        from app.main import discover_models
        up = SimpleNamespace(name="x", prefix="x", models=[], api_key="", discovered_models=[],
                             base_url="https://example.invalid")
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(429, headers={"Retry-After": "300"})
        real_client = httpx.AsyncClient
        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SimpleNamespace(upstreams=[up], data_dir=Path(tmp) / "data", proxy_for=lambda _: None)
            legacy = Path(tmp) / "legacy.json"
            with patch.object(probe, "COOLDOWN_FILE", legacy), \
                 patch.object(probe, "LOCK_FILE", Path(tmp) / "lock"), \
                 patch.object(probe, "HEALTH_FILE", Path(tmp) / "health"), \
                 patch.object(probe, "load_config", return_value=cfg), \
                 patch.object(probe.httpx, "AsyncClient", side_effect=client), \
                 patch.object(probe.time, "time", return_value=1000):
                asyncio.run(discover_models(cfg))
                self.assertEqual(json.loads(probe.cooldown_path(cfg).read_text())["x"], 1300)
                self.assertEqual(probe.main(), 1)
                asyncio.run(discover_models(cfg))
                self.assertEqual(len(requests), 1)
                legacy.write_text('{"x": 1500}')
                self.assertEqual(probe.load_cooldowns(cfg)["x"], 1500)
                self.assertEqual(json.loads(probe.cooldown_path(cfg).read_text())["x"], 1500)
                legacy.write_text('invalid')
                asyncio.run(discover_models(cfg))
                self.assertEqual(len(requests), 1)
                legacy.unlink()
                with probe.probe_lock(cfg) as acquired:
                    self.assertTrue(acquired)
                    asyncio.run(discover_models(cfg))
                self.assertEqual(len(requests), 1)

    def test_discovery_cooldown_write_failure_is_not_swallowed(self):
        import app.probe_models as probe
        from app.main import discover_models
        up = SimpleNamespace(name="x", prefix="x", models=[], api_key="", base_url="https://example.invalid")
        real_client = httpx.AsyncClient
        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(lambda request: httpx.Response(429)), **kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SimpleNamespace(upstreams=[up], data_dir=Path(tmp), proxy_for=lambda _: None)
            with patch.object(probe, "COOLDOWN_FILE", Path(tmp) / "legacy"), \
                 patch.object(probe, "LOCK_FILE", Path(tmp) / "lock"), \
                 patch.object(probe, "load_config", return_value=cfg), \
                 patch.object(probe.httpx, "AsyncClient", side_effect=client), \
                 patch.object(probe, "save_cooldowns", side_effect=OSError("disk full")):
                with self.assertRaises(probe.CooldownWriteError):
                    probe.main()
                with self.assertRaises(probe.CooldownWriteError):
                    asyncio.run(discover_models(cfg))

    def test_no_retry_on_generation_errors_or_long_retry_after(self):
        async def run():
            up = SimpleNamespace(name="x", no_proxy=True)
            for status in ("timeout", "server_error"):
                with patch("app.probe_models.probe_one", new=AsyncMock(return_value=("a", {"status": status}))) as one, \
                     patch("app.probe_models.asyncio.sleep", new=AsyncMock()):
                    await probe_upstream(up, ["a"])
                self.assertEqual(one.await_count, 1)
            with patch("app.probe_models.probe_one", new=AsyncMock(return_value=("a", {"status": "rate_limited", "retry_after": 300}))) as one, \
                 patch("app.probe_models.asyncio.sleep", new=AsyncMock()) as sleep:
                result = await probe_upstream(up, ["a", "b"])
            self.assertEqual(one.await_count, 1)
            sleep.assert_not_awaited()
            self.assertEqual(result["a"]["retry_after"], 300)
        asyncio.run(run())

    def test_retries_and_pacing_without_network(self):
        async def run():
            up = SimpleNamespace(name="x", no_proxy=True)
            responses = [("a", {"status": "rate_limited", "retry_after": 3}),
                         ("a", {"status": "ok"}), ("b", {"status": "ok"})]
            with patch("app.probe_models.probe_one", new=AsyncMock(side_effect=responses)) as one, \
                 patch("app.probe_models.asyncio.sleep", new=AsyncMock()) as sleep:
                result = await probe_upstream(up, ["a", "a", "b"])
            self.assertEqual(list(result), ["a", "b"])
            self.assertEqual(one.await_count, 3)
            self.assertEqual([c.args[0] for c in sleep.await_args_list], [3, 1.0])
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
