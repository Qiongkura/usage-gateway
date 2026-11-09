"""Offline authentication boundary tests."""
import sqlite3
import tempfile
import unittest
from urllib.parse import quote
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.stats import router
from app.config import is_loopback_host, load_config
from app.main import app as gateway


class StatsAuthTest(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.state.cfg = SimpleNamespace(stats_key="统计-secret", admin_key="admin-secret")
        self.app.include_router(router)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_all_stats_routes_default_to_protected(self):
        excluded = {
            "health", "readiness", "admin_checkpoint", "admin_shutdown",
            "diagnostics", "reset_upstream_health",
        }
        for route in router.routes:
            if route.name in excluded:
                continue
            path = route.path.replace("{trace_id}", "example").replace("{name}", "example")
            for method in route.methods:
                with self.subTest(path=path, method=method):
                    response = self.client.request(method, path)
                    self.assertEqual(response.status_code, 401)
                    self.assertNotIn("统计-secret", response.text)
                    self.assertEqual(self.client.request(method, path, headers={"X-Admin-Key": "admin-secret"}).status_code, 401)
                    self.assertEqual(self.client.request(method, path, params={"stats_key": "统计-secret"}).status_code, 401)

    def test_key_and_compatibility(self):
        # Empty storage dependencies are not needed for the upstream health endpoint.
        self.assertEqual(self.client.get('/api/upstreams/health', headers={'X-Stats-Key': 'wrong'}).status_code, 401)
        # Headers must be Latin-1; ASCII keys remain usable and comparisons support Unicode configuration.
        self.app.state.cfg.stats_key = 'stats-secret'
        self.assertEqual(self.client.get('/api/upstreams/health', headers={'X-Stats-Key': 'stats-secret'}).status_code, 200)
        self.app.state.cfg.stats_key = ''
        self.assertEqual(self.client.get('/api/upstreams/health').status_code, 200)
        self.assertEqual(self.client.get('/api/health').status_code, 200)
        self.assertEqual(set(self.client.get('/api/health').json()), {'ok', 'ts', 'version'})

    def test_non_loopback_without_keys_fails_closed(self):
        self.app.state.cfg.host = '0.0.0.0'
        self.app.state.cfg.stats_key = ''
        self.app.state.cfg.admin_key = ''
        self.app.state.tracker = None
        cursor = SimpleNamespace(fetchall=lambda: [])
        conn = SimpleNamespace(execute=lambda *args: cursor)
        self.app.state.store = SimpleNamespace(checkpoint=lambda: None, read_conn=lambda: conn)

        for method, path in (
            ('GET', '/api/upstreams/health'),
            ('GET', '/api/diagnostics'),
            ('POST', '/api/upstreams/example/reset'),
            ('POST', '/api/admin/checkpoint'),
        ):
            with self.subTest(path=path):
                response = self.client.request(method, path)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn('traceback', response.text.lower())

        self.app.state.cfg.stats_key = 'stats-secret'
        self.assertEqual(self.client.get('/api/upstreams/health').status_code, 401)
        self.assertEqual(self.client.get('/api/upstreams/health',
                                         headers={'X-Stats-Key': 'stats-secret'}).status_code, 200)
        self.assertEqual(self.client.post('/api/admin/checkpoint').status_code, 503)

        self.app.state.cfg.stats_key = ''
        self.app.state.cfg.admin_key = 'admin-secret'
        self.assertEqual(self.client.get('/api/upstreams/health').status_code, 503)
        self.assertEqual(self.client.post('/api/admin/checkpoint').status_code, 401)
        self.assertEqual(self.client.post('/api/admin/checkpoint',
                                          headers={'X-Admin-Key': 'admin-secret'}).status_code, 200)

    def test_readiness_reports_database_and_scheduler_state(self):
        class Result:
            def __init__(self, value):
                self.value = value

            def fetchone(self):
                return (self.value,)

        conn = SimpleNamespace(execute=lambda sql: Result(1))
        self.app.state.store = SimpleNamespace(read_conn=lambda: conn)
        self.app.state.sched = SimpleNamespace(running=True)

        response = self.client.get('/api/ready')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['checks'], {'database': 'ok', 'scheduler': 'ok'})
        self.assertTrue(response.json()['ok'])
        self.assertEqual(response.headers['cache-control'], 'no-store')

        self.app.state.sched.running = False
        response = self.client.get('/api/ready')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['checks']['scheduler'], 'unavailable')

        def broken_conn():
            raise sqlite3.OperationalError('not available')

        self.app.state.sched.running = True
        self.app.state.store = SimpleNamespace(read_conn=broken_conn)
        response = self.client.get('/api/ready')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['checks']['database'], 'unavailable')
        self.assertNotIn('not available', response.text)

    def test_encoded_unicode_headers_and_rejection(self):
        key = "统计 é+% !密钥"
        encoded = quote(key, safe="~!*'()-._")
        self.app.state.cfg.stats_key = key
        self.app.state.cfg.admin_key = key
        stats = '/api/upstreams/health'
        admin = '/api/upstreams/example/reset'
        for path, method, header in ((stats, 'GET', 'X-Stats-Key-Encoded'),
                                     (admin, 'POST', 'X-Admin-Key-Encoded'),
                                     ('/api/diagnostics', 'GET', 'X-Admin-Key-Encoded'),
                                     ('/api/admin/checkpoint', 'POST', 'X-Admin-Key-Encoded')):
            # A valid key reaches the handler; missing store may raise, so use a stub.
            cursor = SimpleNamespace(fetchall=lambda: [])
            conn = SimpleNamespace(execute=lambda *args: cursor)
            self.app.state.store = SimpleNamespace(checkpoint=lambda: None, read_conn=lambda: conn)
            for bad in ('%GG', '%', '%FF', encoded + 'x', '%C0%AF'):
                with self.subTest(path=path, bad=bad):
                    self.assertEqual(self.client.request(method, path, headers={header: bad}).status_code, 401)
            self.assertEqual(self.client.request(method, path, headers={header: encoded}).status_code, 200)
        self.assertEqual(self.client.post('/api/admin/checkpoint', params={'admin_key': key}).status_code, 200)
        # Encoded header takes precedence, even if a valid legacy credential is present.
        self.assertEqual(self.client.get(stats, headers={'X-Stats-Key': key.encode('utf-8'),
                                                        'X-Stats-Key-Encoded': '%GG'}).status_code, 401)
        self.app.state.cfg.stats_key = '%E7%BB%9F'
        self.assertEqual(self.client.get(stats, headers={'X-Stats-Key': '%E7%BB%9F'}).status_code, 200)
        self.assertEqual(self.client.get(stats, headers={'X-Stats-Key-Encoded': '%E7%BB%9F'}).status_code, 401)

    def test_admin_boundaries(self):
        path = '/api/upstreams/example/reset'
        for headers in ({}, {'X-Stats-Key': 'stats-secret'}, {'X-Admin-Key': 'wrong'}):
            self.assertEqual(self.client.post(path, headers=headers).status_code, 401)
        self.assertEqual(self.client.post(path, params={'admin_key': 'admin-secret'}).status_code, 401)
        self.assertEqual(self.client.post(path, headers={'X-Admin-Key': 'admin-secret'}).status_code, 200)
        self.assertEqual(self.client.get('/api/diagnostics', headers={'X-Stats-Key': 'wrong'}).status_code, 401)
        self.app.state.cfg.admin_key = ''
        self.assertEqual(self.client.post(path).status_code, 200)

    def test_models_client_route_not_stats_protected(self):
        app = FastAPI()
        route = next(r for r in gateway.routes if getattr(r, 'path', '') == '/v1/models')
        app.router.routes.append(route)
        app.state.cfg = SimpleNamespace(find_key=lambda key: object() if key == 'client-secret' else None,
                                        stats_key='stats-secret', upstreams=[])
        client = TestClient(app)
        response = client.get('/v1/models', headers={'X-Stats-Key': 'stats-secret'})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('invalid stats key', response.text)
        response = client.get('/v1/models', headers={'Authorization': 'Bearer client-secret'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data'], [])

    def test_config_load_preserves_sse(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.yaml'
            path.write_text('server:\n  stats_key: stat-secret\nsse_heartbeat_sec: 3\n', encoding='utf-8')
            cfg = load_config(path)
            self.assertEqual(cfg.stats_key, 'stat-secret')
            self.assertEqual(cfg.sse_heartbeat_sec, 3)
            path.write_text('{}', encoding='utf-8')
            self.assertEqual(load_config(path).stats_key, '')

    def test_loopback_host_detection_is_conservative(self):
        for host in ('127.0.0.1', '127.42.0.9', '::1', '[::1]', 'localhost', 'LOCALHOST.'):
            with self.subTest(host=host):
                self.assertTrue(is_loopback_host(host))
        for host in ('0.0.0.0', '::', '192.168.1.10', 'gateway.example', '127.0.0.1.example'):
            with self.subTest(host=host):
                self.assertFalse(is_loopback_host(host))


if __name__ == '__main__':
    unittest.main()
