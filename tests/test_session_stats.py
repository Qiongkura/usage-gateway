"""Session aggregation, shared log filtering and request outcomes."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_logs_api import make_app, rec
from fastapi.testclient import TestClient


class SessionStatsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app, self.store = make_app(Path(self.temp.name))
        self.client = TestClient(self.app)
        now = self.now = int(time.time())
        self.store.insert_usage([
            rec(ts=now, input_tokens=1000, cost_usd=1),
            rec(ts=now-1, input_tokens=1000),
            rec(ts=now-2, input_tokens=2000, cost_usd=2),
            rec(ts=now-3, input_tokens=3000),
        ])
        conn = self.store.read_conn()
        ids = [r[0] for r in conn.execute('SELECT id FROM usage_records ORDER BY ts DESC')]
        conn.execute("UPDATE usage_records SET session_key='opaque-a', result='stream_interrupted', error_stage='stream' WHERE id=?", (ids[0],))
        conn.execute("UPDATE usage_records SET session_key='opaque-a', result='success' WHERE id=?", (ids[1],))
        conn.execute("UPDATE usage_records SET session_key='opaque-b', result='client_cancelled' WHERE id=?", (ids[2],))
        conn.execute("UPDATE usage_records SET result=NULL, status=500 WHERE id=?", (ids[3],))
        conn.commit()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_sessions_and_logs(self):
        params = {'range': 'all', 'origin': 'all'}
        data = self.client.get('/api/sessions', params={**params, 'page_size': 2}).json()
        self.assertEqual(data['total'], 3)
        self.assertEqual(data['pages'], 2)
        self.assertEqual(data['cost_basis'], 'recorded_only')
        first = data['items'][0]
        self.assertEqual(first['requests'], 2)
        self.assertEqual(first['cost_recorded_usd'], 1)
        self.assertEqual(first['unpriced_requests'], 1)
        logs = self.client.get('/api/logs', params={**params, 'session_key': 'opaque-a'}).json()
        self.assertEqual(logs['total'], 2)
        self.assertGreater(logs['summary']['cost_usd'], 1)
        self.assertEqual(logs['summary']['failed'], 1)
        self.assertEqual(logs['items'][0]['error_stage'], 'stream')
        unassigned = self.client.get('/api/logs', params={**params, 'unassigned': 'true'}).json()
        self.assertEqual(unassigned['total'], 1)
        page = self.client.get('/api/sessions', params={**params, 'page_size': 2, 'page': 2}).json()
        self.assertIsNone(page['items'][0]['session_key'])

    def test_outcomes_and_shared_filter(self):
        base = {'range': 'all', 'origin': 'all'}
        for status, count in [('success', 1), ('error', 2), ('cancelled', 1)]:
            params = {**base, 'status': status}
            logs = self.client.get('/api/logs', params=params).json()
            sessions = self.client.get('/api/sessions', params=params).json()
            self.assertEqual(logs['total'], count)
            self.assertEqual(sum(x['requests'] for x in sessions['items']), count)
        summary = self.client.get('/api/logs', params=base).json()['summary']
        self.assertEqual(summary['failed'], 2)
        self.assertEqual(summary['cancelled'], 1)

    def test_outcomes_are_consistent_across_dashboard_and_speed(self):
        now = self.now
        self.store.insert_usage([
            rec(ts=now-4, output_tokens=100, latency_ms=1000),
            rec(ts=now-5, output_tokens=100, latency_ms=1000),
            rec(ts=now-6, output_tokens=100, latency_ms=1000),
            rec(ts=now-600-1, output_tokens=100, latency_ms=1000),
            rec(ts=now+1, output_tokens=100, latency_ms=1000),
        ])
        conn = self.store.read_conn()
        conn.execute("UPDATE usage_records SET result='unknown' WHERE ts=?", (now-4,))
        conn.execute("UPDATE usage_records SET result=NULL, status=NULL WHERE ts=?", (now-5,))
        conn.execute("UPDATE usage_records SET result=NULL, status=200 WHERE ts=?", (now-6,))
        conn.execute("UPDATE usage_records SET latency_ms=1000, output_tokens=100 WHERE ts BETWEEN ? AND ?", (now-3, now))
        conn.commit()
        with patch('app.api.stats.time.time', return_value=now):
            base = {'range': '1d', 'origin': 'all'}
            logs = self.client.get('/api/logs', params=base).json()
            summary = self.client.get('/api/summary', params=base).json()
            timeline = self.client.get('/api/health-timeline', params={'minutes': 10, 'bucket_sec': 120}).json()
            for status, count in [('success', 2), ('error', 2), ('cancelled', 1), ('unknown', 2)]:
                self.assertEqual(self.client.get('/api/logs', params={**base, 'status': status}).json()['total'], count + (1 if status == 'success' else 0))
            self.assertEqual((logs['summary']['failed'], logs['summary']['cancelled'], logs['summary']['unknown']), (2, 1, 2))
            self.assertEqual((timeline['total_requests'], timeline['total_ok'], timeline['total_failed'], timeline['total_cancelled'], timeline['total_unknown']), (7, 2, 2, 1, 2))
            self.assertEqual(timeline['success_rate'], 50)
            self.assertEqual((summary['successful_requests'], summary['failed_requests'], summary['cancelled_requests'], summary['unknown_requests']), (3, 2, 1, 2))
            self.assertEqual(summary['success_rate'], 60)
            self.assertEqual(summary['tok_per_sec'], 100)
            speed_times = {row['ts'] for row in self.client.get('/api/speed').json()['history']}
            self.assertEqual(speed_times, {now-1, now-6, now-601, now+1})

    def test_no_decidable_outcomes_have_no_success_rate(self):
        conn = self.store.read_conn()
        conn.execute("UPDATE usage_records SET result='client_cancelled'")
        conn.commit()
        summary = self.client.get('/api/summary', params={'range':'1d'}).json()
        timeline = self.client.get('/api/health-timeline', params={'minutes':10}).json()
        self.assertIsNone(summary['success_rate'])
        self.assertIsNone(timeline['success_rate'])
        self.assertTrue(all(b['level'] == 4 for b in timeline['buckets'] if b['total']))

    def test_trace_errors_respect_structured_outcomes(self):
        conn = self.store.read_conn()
        for i, (status, result) in enumerate([
            (200, 'stream_interrupted'), (500, 'success'), (500, 'client_cancelled'),
            (500, 'unknown'), (500, None), (200, 'new_error'), (None, None),
        ]):
            conn.execute(
                "INSERT INTO request_traces (trace_id, ts, status, result, spans_json) VALUES (?, ?, ?, ?, ?)",
                (f'trace-{i}', self.now - i, status, result, '[]'),
            )
        conn.commit()
        traces = self.client.get('/api/traces', params={'errors_only': 'true'}).json()
        self.assertEqual({t['trace_id'] for t in traces}, {'trace-0', 'trace-4', 'trace-5'})

    def test_speed_excludes_missing_output_but_keeps_zero(self):
        conn = self.store.read_conn()
        conn.execute("UPDATE usage_records SET result='success', latency_ms=1000, output_tokens=NULL")
        conn.execute("UPDATE usage_records SET output_tokens=0 WHERE ts=?", (self.now,))
        conn.commit()
        summary = self.client.get('/api/summary', params={'range': '1d'}).json()
        speed = self.client.get('/api/speed').json()
        self.assertEqual(summary['tok_per_sec'], 0)
        self.assertEqual(summary['avg_latency_ms'], 1000)
        self.assertEqual(speed['recent_count'], 1)
        self.assertEqual(speed['history'][0]['output_tokens'], 0)
        self.assertEqual(speed['history'][0]['tok_per_sec'], 0)


if __name__ == '__main__':
    unittest.main()
