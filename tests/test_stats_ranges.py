"""Regression coverage for continuous ranges and consistent token totals."""
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from test_logs_api import make_app, rec


class StatsRangesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.store = make_app(Path(self.tmp.name))
        self.client = TestClient(self.app)
        self.now = int(time.time())
        self.clock = patch('app.api.stats.time.time', return_value=self.now)
        self.clock.start()
        self.store.insert_usage([
            rec(ts=self.now - 3600, input_tokens=10, output_tokens=20,
                cache_read=30, cache_write=40, reasoning=15),
            rec(ts=self.now - 5 * 3600, source='other', input_tokens=1,
                output_tokens=2, cache_read=3, cache_write=4, reasoning=1),
        ])

    def tearDown(self):
        self.clock.stop()
        self.client.close()
        self.store.close()
        self.tmp.cleanup()

    def test_continuous_ranges_and_token_totals(self):
        for range_, count in [('1d', 25), ('3d', 73), ('7d', 169)]:
            with self.subTest(range=range_):
                params = {'range': range_}
                response = self.client.get('/api/timeseries', params={**params, 'bucket': 'hour'})
                self.assertEqual(response.status_code, 200)
                series = response.json()
                self.assertEqual(len(series), count)
                dates = [datetime.strptime(row['bucket'], '%Y-%m-%d %H:%M') for row in series]
                self.assertTrue(all(b - a == timedelta(hours=1) for a, b in zip(dates, dates[1:])))
                self.assertTrue(any(row['requests'] == 0 and row['input'] == 0 for row in series))
                chart_total = sum(sum(row[k] for k in ('input', 'output', 'cache_read', 'cache_write')) for row in series)
                summary = self.client.get('/api/summary', params=params)
                usage = self.client.get('/api/usage', params={**params, 'group_by': 'model'})
                logs = self.client.get('/api/logs', params={**params, 'origin': 'all'})
                for response in (summary, usage, logs):
                    self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(chart_total, 110)
                self.assertEqual(summary.json()['total_tokens'], chart_total)
                self.assertEqual(sum(row['tokens'] for row in usage.json()), chart_total)
                self.assertEqual(logs.json()['summary']['total_tokens'], chart_total)

    def test_today_bounds_use_the_same_clock_as_the_end(self):
        fixed = int(datetime(2024, 5, 4, 0, 0, 5).timestamp())
        with patch('app.api.stats.time.time', return_value=fixed):
            self.store.insert_usage([
                rec(ts=fixed - 6, input_tokens=999, raw={'request_id': 'before-today'}),
                rec(ts=fixed - 5, input_tokens=17, raw={'request_id': 'today-start'}),
                rec(ts=fixed, input_tokens=23, raw={'request_id': 'today-end'}),
                rec(ts=fixed + 1, input_tokens=999, raw={'request_id': 'after-now'}),
            ])
            snapshot = self.client.get('/api/overview-usage', params={'range': 'today'}).json()
        self.assertEqual(snapshot['summary']['total_tokens'], 40)
        self.assertEqual(sum(row['input'] for row in snapshot['timeseries']), 40)
        self.assertEqual(sum(row['tokens'] for row in snapshot['model_timeseries']), 40)

    def test_hour_buckets_identify_partial_window_edges(self):
        from app.api.stats import _hour_bucket_start

        hour = _hour_bucket_start(self.now)
        for offset in (0, 1800, 3599):
            with self.subTest(offset=offset), patch('app.api.stats.time.time', return_value=hour + offset):
                response = self.client.get('/api/overview-usage', params={'range': '1d'})
                self.assertEqual(response.status_code, 200, response.text)
                series = response.json()['timeseries']
                self.assertEqual(len(series), 25)
                self.assertEqual(series[0]['partial'], offset > 0)
                self.assertTrue(all(not row['partial'] for row in series[1:-1]))
                self.assertEqual(series[-1]['partial'], offset < 3599)

    def test_all_range_excludes_records_before_epoch_and_after_now(self):
        self.store.insert_usage([
            rec(ts=-1, input_tokens=1000, raw={'request_id': 'before-epoch'}),
            rec(ts=0, input_tokens=7, raw={'request_id': 'epoch'}),
            rec(ts=self.now + 1, input_tokens=1000, raw={'request_id': 'future'}),
        ])
        snapshot = self.client.get('/api/overview-usage', params={'range': 'all'}).json()
        self.assertEqual(snapshot['summary']['total_tokens'], 117)
        self.assertEqual(sum(sum(row[k] for k in ('input', 'output', 'cache_read', 'cache_write'))
                             for row in snapshot['timeseries']), 117)
        self.assertEqual(sum(row['tokens'] for row in snapshot['model_timeseries']), 117)

    def test_source_and_model_filter_do_not_mix_sources(self):
        response = self.client.get('/api/timeseries', params={
            'range': '1d', 'bucket': 'hour', 'source': 'mistral',
            'model': rec().model,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sum(row['requests'] for row in response.json()), 1)
        self.assertEqual(sum(row['cache_write'] for row in response.json()), 40)

    def test_model_totals_match_source_breakdown(self):
        self.store.insert_usage([
            rec(ts=self.now, source='other', input_tokens=25, output_tokens=5),
            rec(ts=self.now, source='third', input_tokens=50, cache_read=10),
        ])
        response = self.client.get('/api/overview-usage', params={'range': '1d'})
        self.assertEqual(response.status_code, 200, response.text)
        snapshot = response.json()
        model = next(row for row in snapshot['models'] if row['grp'] == rec().model)
        sources = [row for row in snapshot['active_models'] if row['model'] == model['grp']]
        self.assertEqual({row['source'] for row in sources}, {'mistral', 'other', 'third'})
        self.assertEqual(sum(row['requests'] for row in sources), model['requests'])
        self.assertEqual(sum(row['tokens'] for row in sources), model['tokens'])

    def test_chart_picker_and_model_table_reconcile_at_range_edges(self):
        self.store.insert_usage([
            rec(ts=self.now - 86400, source='first', model='shared', input_tokens=10,
                raw={'request_id': 'edge-start'}),
            rec(ts=self.now - 3600, source='first', model='shared', input_tokens=20,
                cache_write=5, raw={'request_id': 'first-middle'}),
            rec(ts=self.now, source='second', model='shared', input_tokens=30,
                raw={'request_id': 'edge-end'}),
            rec(ts=self.now - 86401, source='first', model='shared', input_tokens=999,
                raw={'request_id': 'before'}),
            rec(ts=self.now + 1, source='second', model='shared', input_tokens=999,
                raw={'request_id': 'after'}),
        ])
        snapshot = self.client.get('/api/overview-usage', params={'range': '1d'}).json()
        table = next(row for row in snapshot['models'] if row['grp'] == 'shared')
        picker = [row for row in snapshot['active_models'] if row['model'] == 'shared']
        self.assertEqual((table['requests'], table['tokens']), (3, 65))
        self.assertEqual((sum(row['requests'] for row in picker), sum(row['tokens'] for row in picker)), (3, 65))
        self.assertTrue(any(row['requests'] == 0 for row in snapshot['timeseries']))
        for item in picker:
            points = [row for row in snapshot['model_timeseries']
                      if row['source'] == item['source'] and row['model'] == item['model']]
            self.assertEqual(sum(row['requests'] for row in points), item['requests'])
            self.assertEqual(sum(row['tokens'] for row in points), item['tokens'])

    def test_summary_speed_and_outcomes_with_mixed_records(self):
        rows = [
            ('a', 'success', 200, 100, 1000),
            ('a', None, 200, 50, 500),
            ('a', 'stream_interrupted', 200, 999, 100),
            ('b', 'success', 200, 60, 2000),
            ('b', 'success', 200, 10, None),
            ('b', 'client_cancelled', 200, 999, 100),
            ('b', 'unknown', None, 999, 100),
            ('b', None, 500, 999, 100),
        ]
        self.store.insert_usage([
            rec(ts=self.now, source=source, result=result, status=status,
                output_tokens=output, latency_ms=latency,
                raw={'request_id': f'mixed-{index}'})
            for index, (source, result, status, output, latency) in enumerate(rows)
        ])
        summary = self.client.get('/api/summary', params={'range': '1d'}).json()
        self.assertEqual((summary['successful_requests'], summary['failed_requests'],
                          summary['cancelled_requests'], summary['unknown_requests']), (6, 2, 1, 1))
        self.assertEqual(summary['tok_per_sec'], 60)
        self.assertEqual(summary['avg_latency_ms'], 1166.7)
        sources = {item['source']: item for item in summary['by_source']}
        self.assertEqual(sources['a']['tok_per_sec'], 100)
        self.assertEqual(sources['b']['tok_per_sec'], 30)
        self.assertNotIn('speed_count', sources['a'])
        with patch('app.api.stats.time.time', return_value=self.now + 2 * 86400):
            empty = self.client.get('/api/summary', params={'range': '1d'}).json()
        self.assertEqual((empty['total_requests'], empty['tok_per_sec'], empty['avg_latency_ms']), (0, 0, 0))

    def test_summary_distinguishes_collector_from_named_direct_key(self):
        self.store.insert_usage([
            rec(ts=self.now, source='collector-test', client_key=None, input_tokens=10,
                raw={'request_id': 'null-key'}),
            rec(ts=self.now, source='gateway-test', client_key='(direct)', input_tokens=20,
                raw={'request_id': 'direct-key'}),
        ])
        summary = self.client.get('/api/overview-usage', params={'range': '1d'}).json()['summary']
        rows = [row for row in summary['by_client_key'] if row['client_key'] == '(direct)']
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['is_collector']: row['tokens'] for row in rows}, {1: 10, 0: 20})

    def test_client_key_usage_and_details_distinguish_named_unknown(self):
        self.store.insert_usage([
            rec(ts=self.now, source='key-test', client_key=None, input_tokens=10,
                raw={'request_id': 'unknown-null'}),
            rec(ts=self.now, source='key-test', client_key='(unknown)', input_tokens=20,
                raw={'request_id': 'unknown-named'}),
        ])
        params = {'range': '1d', 'source': 'key-test'}
        response = self.client.get('/api/usage', params={**params, 'group_by': 'client_key'})
        self.assertEqual(response.status_code, 200, response.text)
        rows = response.json()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['grp'] == '(unknown)' for row in rows))
        self.assertEqual({row['is_collector']: (row['requests'], row['tokens']) for row in rows},
                         {1: (1, 10), 0: (1, 20)})
        for endpoint in ('/api/logs', '/api/sessions'):
            response = self.client.get(endpoint, params={**params, 'origin': 'all',
                                                         'client_key': '(unknown)'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['total'], 1)
        self.assertEqual(self.client.get('/api/logs', params={**params, 'origin': 'all',
                         'client_key': '(unknown)'}).json()['summary']['total_tokens'], 20)

    def test_overview_model_breakdown_matches_independent_endpoints(self):
        self.store.insert_usage([
            rec(ts=self.now, source='other', model=None, input_tokens=0, cost_usd=None,
                raw={'request_id': 'benchmark-null'}),
            rec(ts=self.now, source='other', model='', input_tokens=7, output_tokens=2,
                cache_read=3, cache_write=4, cost_usd=0.0,
                raw={'request_id': 'benchmark-empty'}),
            rec(ts=self.now, source='other', model='same', input_tokens=7, output_tokens=2,
                cache_read=3, cache_write=4, cost_usd=0.12345678,
                raw={'request_id': 'benchmark-same'}),
            rec(ts=self.now, source='third', model='same', input_tokens=7, output_tokens=2,
                cache_read=3, cache_write=4, cost_usd=0.12345678,
                raw={'request_id': 'benchmark-tie'}),
        ])
        params = {'range': '1d'}
        snapshot = self.client.get('/api/overview-usage', params=params).json()
        self.assertEqual(snapshot['active_models'], self.client.get('/api/models/active', params=params).json())
        self.assertEqual(snapshot['costs'], self.client.get('/api/costs', params=params).json())

    def test_missing_and_literal_unknown_model_reconcile(self):
        self.store.insert_usage([
            rec(ts=self.now, source='first', model=None, input_tokens=10,
                raw={'request_id': 'missing-first'}),
            rec(ts=self.now, source='first', model='(unknown)', input_tokens=20,
                raw={'request_id': 'literal-first'}),
            rec(ts=self.now, source='second', model=None, input_tokens=30,
                raw={'request_id': 'missing-second'}),
            rec(ts=self.now, source='second', model='', input_tokens=40,
                raw={'request_id': 'empty-second'}),
        ])
        response = self.client.get('/api/overview-usage', params={'range': '1d'})
        self.assertEqual(response.status_code, 200, response.text)
        snapshot = response.json()
        unknown = next(row for row in snapshot['models'] if row['grp'] == '(unknown)')
        parts = [row for row in snapshot['active_models'] if row['model'] in (None, '(unknown)')]
        self.assertEqual((unknown['requests'], unknown['tokens']), (3, 60))
        self.assertEqual((sum(row['requests'] for row in parts), sum(row['tokens'] for row in parts)), (3, 60))
        self.assertEqual(next(row for row in snapshot['models'] if row['grp'] == '')['tokens'], 40)
        for item in parts:
            buckets = [row for row in snapshot['model_timeseries']
                       if row['source'] == item['source'] and row['model'] == item['model']]
            self.assertEqual(sum(row['requests'] for row in buckets), item['requests'])
            self.assertEqual(sum(row['tokens'] for row in buckets), item['tokens'])

    def test_health_ranges_are_continuous_and_respect_exact_bounds(self):
        for minutes, bucket in ((1440, 120), (4320, 1200), (10080, 1800)):
            with self.subTest(minutes=minutes):
                start = self.now - minutes * 60
                source = f'health-{minutes}'
                self.store.insert_usage([
                    rec(ts=ts, source=source, input_tokens=value,
                        raw={'request_id': f'{source}-{ts}'})
                    for ts, value in ((start-1, 10000), (start, 100),
                                      (self.now, 200), (self.now+1, 20000))
                ])
                response = self.client.get('/api/health-timeline', params={'minutes':minutes, 'bucket_sec':bucket})
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                buckets = data['buckets']
                self.assertTrue(all(b['ts'] - a['ts'] == bucket for a, b in zip(buckets, buckets[1:])))
                self.assertTrue(all(row['partial'] == (start > row['ts'] or self.now < row['ts'] + bucket - 1)
                                    for row in buckets))
                self.assertLessEqual(len(buckets), 721)
                self.assertTrue(any(row['total'] == 0 for row in buckets))
                self.assertEqual(data['minutes'], minutes)
                self.assertRegex(buckets[0]['time'], r'^\d{2}-\d{2} \d{2}:\d{2}$')
                self.assertNotEqual(buckets[0]['time'], buckets[-1]['time'])
                # Other subtests also insert records; isolate the new boundary contribution.
                included = self.store.read_conn().execute(
                    'SELECT SUM(input_tokens) FROM usage_records WHERE source=? AND ts BETWEEN ? AND ?',
                    (source, start, self.now)).fetchone()[0]
                self.assertEqual(included, 300)
                logs = self.client.get('/api/logs', params={'range':{1440:'1d',4320:'3d',10080:'7d'}[minutes], 'origin':'all'}).json()
                self.assertEqual(data['total_requests'], logs['total'])
                self.assertEqual(sum(row['tokens'] for row in buckets), logs['summary']['total_tokens'])
                response = self.client.get('/api/overview-usage', params={
                    'range': {1440: '1d', 4320: '3d', 10080: '7d'}[minutes],
                })
                self.assertEqual(response.status_code, 200, response.text)
                overview = response.json()
                health = overview['health_timeline']
                summary = overview['summary']
                self.assertEqual(health['minutes'], minutes)
                self.assertEqual(health['bucket_sec'], bucket)
                self.assertEqual(health['end'], summary['end'])
                self.assertEqual(health['total_requests'], summary['total_requests'])
                self.assertEqual(health['total_ok'], summary['successful_requests'])
                self.assertEqual(health['total_failed'], summary['failed_requests'])
                self.assertEqual(health['total_cancelled'], summary['cancelled_requests'])
                self.assertEqual(health['total_unknown'], summary['unknown_requests'])
                self.assertEqual(health['success_rate'], summary['success_rate'])
                self.assertEqual(sum(row['tokens'] for row in health['buckets']), summary['total_tokens'])

    def test_health_partial_bucket_edges_when_aligned_or_unaligned(self):
        bucket = 120
        base = self.now - self.now % bucket
        for offset in (0, 30, 119):
            with self.subTest(offset=offset), patch('app.api.stats.time.time', return_value=base + offset):
                data = self.client.get('/api/health-timeline', params={
                    'minutes': 10, 'bucket_sec': bucket,
                }).json()
                self.assertEqual(data['buckets'][0]['partial'], offset > 0)
                self.assertTrue(all(not row['partial'] for row in data['buckets'][1:-1]))
                self.assertEqual(data['buckets'][-1]['partial'], offset < bucket - 1)

    def test_rolling_range_includes_only_records_within_exact_bounds(self):
        for range_, seconds in [('1d', 86400), ('3d', 3 * 86400), ('7d', 7 * 86400)]:
            with self.subTest(range=range_):
                source = 'boundary-' + range_
                start = self.now - seconds
                self.store.insert_usage([
                    rec(ts=ts, source=source, model=source, input_tokens=tokens,
                        raw={'request_id': f'{source}-{ts}'})
                    for ts, tokens in [(start - 1, 1000), (start, 10),
                                       (self.now, 20), (self.now + 1, 2000)]
                ])
                snapshot = self.client.get('/api/overview-usage', params={'range': range_}).json()
                model = next(row for row in snapshot['models'] if row['grp'] == source)
                series = [row for row in snapshot['model_timeseries'] if row['source'] == source]
                self.assertEqual(model['tokens'], 30)
                self.assertEqual(sum(row['tokens'] for row in series), 30)
                self.assertEqual(sum(row['by_source'].get(source, 0) for row in snapshot['timeseries']), 30)

    def test_price_coverage_is_reported_by_bucket_and_client_key(self):
        self.store.insert_usage([
            rec(ts=self.now - 3601, source='mistral', client_key='default',
                input_tokens=10, cost_usd=0),
            rec(ts=self.now - 5 * 3600 - 1, source='other', client_key='other',
                input_tokens=10, cost_usd=1),
        ])
        response = self.client.get('/api/overview-usage', params={'range': '1d'})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data['summary']['priced_requests'], 2)
        self.assertEqual(data['summary']['unpriced_requests'], 2)
        keys = {row['client_key']: row for row in data['summary']['by_client_key']}
        self.assertEqual(keys['default']['requests'], 3)
        self.assertEqual(keys['default']['priced_requests'], 1)
        self.assertEqual(keys['other']['priced_requests'], 1)
        self.assertEqual(sum(row['priced_requests'] for row in data['timeseries']), 2)
        self.assertTrue(any(row['requests'] > row['priced_requests'] for row in data['timeseries']))
        self.assertTrue(all(row['priced_requests'] == 0 for row in data['timeseries'] if not row['requests']))

    def test_small_costs_are_preserved_across_overview_breakdowns(self):
        self.store.insert_usage([
            rec(ts=self.now - 10, source='small-a', input_tokens=1, cost_usd=0.0000004),
            rec(ts=self.now - 20, source='small-b', input_tokens=1, cost_usd=0.0000004),
            rec(ts=self.now - 30, source='small-c', input_tokens=1, cost_usd=0),
        ])
        response = self.client.get('/api/overview-usage', params={'range': '1d'})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertAlmostEqual(data['summary']['total_cost_usd'], 0.0000008, places=12)
        self.assertEqual(data['summary']['priced_requests'], 3)
        self.assertEqual(data['summary']['unpriced_requests'], 2)
        self.assertAlmostEqual(sum(row['cost_usd'] for row in data['summary']['by_source']), 0.0000008, places=12)
        self.assertAlmostEqual(sum(row['cost'] for row in data['summary']['by_client_key']), 0.0000008, places=12)
        self.assertAlmostEqual(sum(row['cost'] for row in data['costs']), 0.0000008, places=12)
        for row in data['costs']:
            self.assertAlmostEqual(row['avg_cost_per_request'], row['cost'] / row['requests'], places=12)

    def test_timeseries_preserves_small_costs_in_separate_buckets(self):
        self.store.insert_usage([
            rec(ts=self.now - 10, source='small-a', input_tokens=1, cost_usd=0.0000004),
            rec(ts=self.now - 7200, source='small-b', input_tokens=1, cost_usd=0.0000004),
        ])
        for range_, bucket in (('1d', 'hour'), ('7d', 'hour'), ('30d', 'day')):
            with self.subTest(range=range_, bucket=bucket):
                series = self.client.get('/api/timeseries', params={
                    'range': range_, 'bucket': bucket,
                }).json()
                self.assertAlmostEqual(sum(row['cost'] for row in series), 0.0000008, places=12)
                self.assertTrue(all(row['cost'] > 0 for row in series if row['priced_requests']))
                overview = self.client.get('/api/overview-usage', params={'range': range_}).json()
                self.assertAlmostEqual(sum(row['cost'] for row in overview['timeseries']), 0.0000008, places=12)

    def test_heatmaps_exclude_future_records(self):
        self.store.insert_usage([
            rec(ts=self.now, source='heat-now', input_tokens=100),
            rec(ts=self.now + 1, source='heat-future', input_tokens=10000),
        ])
        response = self.client.get('/api/overview-usage', params={'range': '1d'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(sum(row['tokens'] for row in response.json()['summary']['heatmap']), 210)
        response = self.client.get('/api/heatmap', params={'days': 7})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(sum(row['tokens'] for row in response.json()), 210)

    def test_overview_heatmap_starts_at_local_midnight(self):
        from app.api import stats
        end = datetime.fromtimestamp(self.now)
        first_day = (end - timedelta(days=364)).replace(hour=0, minute=0, second=0, microsecond=0)
        start = int(first_day.timestamp())
        self.store.insert_usage([
            rec(ts=start - 1, source='before-first-day', input_tokens=10000),
            rec(ts=start, source='first-day', input_tokens=100),
        ])
        with patch.object(stats, '_bounds', return_value=(self.now - 86400, self.now)):
            response = self.client.get('/api/overview-usage', params={'range': '1d'})
        self.assertEqual(response.status_code, 200, response.text)
        summary = response.json()['summary']
        self.assertEqual(summary['heatmap_end_date'], end.strftime('%Y-%m-%d'))
        first = next(row for row in summary['heatmap'] if row['date'] == first_day.strftime('%Y-%m-%d'))
        self.assertEqual(first['tokens'], 100)
        self.assertEqual(sum(row['tokens'] for row in summary['heatmap']), 210)
        independent = self.client.get('/api/heatmap', params={'days': 365})
        self.assertEqual(independent.status_code, 200, independent.text)
        self.assertEqual([(row['date'], row['tokens']) for row in independent.json()],
                         [(row['date'], row['tokens']) for row in summary['heatmap']])

    def test_null_token_components_are_counted_consistently(self):
        columns = ('input_tokens', 'output_tokens', 'cache_read', 'cache_write')
        timestamps = [self.now - 100 - i for i in range(5)]
        self.store.insert_usage([
            rec(ts=ts, input_tokens=10, output_tokens=20, cache_read=30, cache_write=40)
            for ts in timestamps
        ])
        with self.store.lock:
            for column, ts in zip(columns, timestamps):
                self.store.conn.execute(f'UPDATE usage_records SET {column} = NULL WHERE ts = ?', (ts,))
            self.store.conn.execute(
                'UPDATE usage_records SET input_tokens = NULL, output_tokens = NULL, '
                'cache_read = NULL, cache_write = NULL WHERE ts = ?', (timestamps[-1],))
            self.store.conn.commit()

        snapshot_response = self.client.get('/api/overview-usage', params={'range': '1d'})
        self.assertEqual(snapshot_response.status_code, 200, snapshot_response.text)
        snapshot = snapshot_response.json()
        total = 410
        self.assertEqual(snapshot['summary']['total_tokens'], total)
        self.assertEqual(sum(row['tokens'] for row in snapshot['summary']['by_client_key']), total)
        self.assertEqual(sum(row['tokens'] for row in snapshot['summary']['heatmap']), total)
        self.assertEqual(sum(row['tokens'] for row in snapshot['models']), total)
        self.assertEqual(sum(row['tokens'] for row in snapshot['active_models']), total)
        self.assertEqual(sum(row['tokens'] for row in snapshot['model_timeseries']), total)
        self.assertEqual(sum(sum(row[k] for k in ('input', 'output', 'cache_read', 'cache_write'))
                             for row in snapshot['timeseries']), total)
        for group in ('model', 'client_key'):
            response = self.client.get('/api/usage', params={'range': '1d', 'group_by': group})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(sum(row['tokens'] for row in response.json()), total)
        logs = self.client.get('/api/logs', params={'range': '1d', 'origin': 'all'})
        self.assertEqual(logs.status_code, 200, logs.text)
        self.assertEqual(logs.json()['summary']['total_tokens'], total)
        self.assertEqual(sum(row['total_tokens'] for row in logs.json()['items']), total)
        self.assertEqual(logs.json()['summary']['backfilled_requests'], 5)
        heatmap = self.client.get('/api/heatmap', params={'days': 7})
        self.assertEqual(heatmap.status_code, 200, heatmap.text)
        self.assertEqual(sum(row['tokens'] for row in heatmap.json()), total)
        health = self.client.get('/api/health-timeline', params={'minutes': 120})
        self.assertEqual(health.status_code, 200, health.text)
        self.assertEqual(sum(row['tokens'] for row in health.json()['buckets']), 400)

    def test_failed_unbilled_and_gateway_entry_failures_are_not_unpriced(self):
        with self.store.lock:
            self.store.conn.execute('DELETE FROM usage_records')
            self.store.conn.commit()
        self.store.insert_usage([
            rec(ts=self.now - 1, source='upstream-a', input_tokens=10,
                cost_usd=1, status=200),
            rec(ts=self.now - 2, source='upstream-b', status=504),
            rec(ts=self.now - 3, source='gateway', status=500),
            rec(ts=self.now - 4, source='upstream-b', client_key='key-b',
                input_tokens=10, status=502, result='stream_interrupted'),
        ])

        response = self.client.get('/api/overview-usage', params={'range': '1d'})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        summary = data['summary']
        self.assertEqual(summary['total_requests'], 4)
        self.assertEqual(summary['successful_requests'], 1)
        self.assertEqual(summary['failed_requests'], 3)
        self.assertEqual(summary['priced_requests'], 1)
        self.assertEqual(summary['billable_requests'], 2)
        self.assertEqual(summary['unpriced_requests'], 1)
        self.assertEqual(summary['failed_unbilled_requests'], 2)
        self.assertEqual(summary['gateway_entry_failures'], 1)
        self.assertAlmostEqual(summary['total_cost_usd'], 1.0)
        self.assertEqual({row['source'] for row in summary['by_source']},
                         {'upstream-a', 'upstream-b'})
        self.assertTrue(all(
            'gateway' not in row.get('by_source', {}) for row in data['models']
        ))
        self.assertNotIn('gateway', {row['source'] for row in data['costs']})

        gateway_logs = self.client.get('/api/logs', params={
            'range': '1d', 'source': 'gateway', 'origin': 'all',
        })
        self.assertEqual(gateway_logs.status_code, 200, gateway_logs.text)
        gateway_item = gateway_logs.json()['items'][0]
        self.assertEqual(gateway_item['source'], 'gateway')
        self.assertEqual(gateway_item['cost_source'], 'not_applicable')
        self.assertIsNone(gateway_item['cost_usd'])

    def test_overview_uses_one_snapshot_even_when_record_arrives_mid_query(self):
        from app.api import stats

        original = stats._summary
        def insert_after_summary(*args):
            result = original(*args)
            self.store.insert_usage([rec(ts=self.now, input_tokens=900)])
            return result

        with patch.object(stats, '_summary', side_effect=insert_after_summary):
            response = self.client.get('/api/overview-usage', params={'range': '3d'})
        self.assertEqual(response.status_code, 200, response.text)
        snapshot = response.json()
        self.assertEqual(snapshot['summary']['total_tokens'], 110)
        self.assertEqual(snapshot['health_timeline']['total_requests'], snapshot['summary']['total_requests'])
        self.assertEqual(sum(row['tokens'] for row in snapshot['health_timeline']['buckets']), 110)
        self.assertEqual(snapshot['health_timeline']['end'], snapshot['summary']['end'])
        self.assertEqual(sum(row['tokens'] for row in snapshot['models']), 110)
        self.assertEqual(sum(row['tokens'] for row in snapshot['active_models']), 110)
        self.assertEqual(sum(sum(row[k] for k in ('input', 'output', 'cache_read', 'cache_write'))
                             for row in snapshot['timeseries']), 110)
        self.assertEqual(sum(row['requests'] for row in snapshot['costs']), 2)
        self.assertEqual(sum(row['tokens'] for row in snapshot['model_timeseries']), 110)
        self.assertEqual({(row['source'], row['tokens']) for row in snapshot['model_timeseries']},
                         {('mistral', 100), ('other', 10)})
        for bucket in snapshot['timeseries']:
            model_rows = [row for row in snapshot['model_timeseries'] if row['bucket'] == bucket['bucket']]
            self.assertEqual(sum(row['requests'] for row in model_rows), bucket['requests'])
            self.assertEqual(sum(row['tokens'] for row in model_rows),
                             sum(bucket[k] for k in ('input', 'output', 'cache_read', 'cache_write')))
        fresh = self.client.get('/api/overview-usage', params={'range': '3d'})
        self.assertEqual(fresh.json()['summary']['total_tokens'], 1010)
        self.assertEqual(sum(row['tokens'] for row in fresh.json()['health_timeline']['buckets']), 1010)
        self.assertEqual(sum(row['tokens'] for row in fresh.json()['model_timeseries']), 1010)

    def test_logs_and_sessions_use_one_snapshot_during_writes(self):
        original_read_conn = self.store.read_conn
        inserted_records = []
        test = self
        for endpoint, marker in (('/api/logs', 'SELECT COUNT(*) AS requests'),
                                 ('/api/sessions', 'SELECT COUNT(*) FROM (SELECT session_key')):
            with self.subTest(endpoint=endpoint):
                inserted = []
                class Cursor:
                    def __init__(self, cursor):
                        self.cursor = cursor
                    def fetchone(self):
                        row = self.cursor.fetchone()
                        if not inserted:
                            test.store.insert_usage([rec(
                                ts=test.now - 30 - len(inserted_records),
                                input_tokens=1000, cache_read=1000,
                                session_key='session-' + str(len(inserted_records)))])
                            inserted.append(True)
                            inserted_records.append(True)
                        return row
                    def __getattr__(self, name):
                        return getattr(self.cursor, name)
                class Connection:
                    def __init__(self, conn):
                        self.conn = conn
                    def execute(self, sql, *args):
                        cursor = self.conn.execute(sql, *args)
                        return Cursor(cursor) if marker in sql else cursor
                    def __getattr__(self, name):
                        return getattr(self.conn, name)
                with patch.object(self.store, 'read_conn', side_effect=lambda: Connection(original_read_conn())):
                    response = self.client.get(endpoint, params={'range': '1d', 'origin': 'all'})
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertTrue(inserted)
                if endpoint == '/api/logs':
                    self.assertEqual(data['total'], 2)
                    self.assertEqual(data['summary']['requests'], 2)
                    self.assertEqual(len(data['items']), 2)
                    self.assertEqual(sum(row['total_tokens'] for row in data['items']), 110)
                    self.assertEqual(data['summary']['backfilled_requests'], 1)
                    self.assertAlmostEqual(data['summary']['cost_computed_usd'], 0.000286)
                    self.assertAlmostEqual(data['summary']['cache_saved_usd'], 0.000054)
                else:
                    self.assertEqual(data['total'], 2)
                    self.assertEqual(len(data['items']), 2)
                    self.assertEqual(sum(row['requests'] for row in data['items']), 3)
                fresh = self.client.get(endpoint, params={'range': '1d', 'origin': 'all'}).json()
                self.assertEqual(fresh['total'], data['total'] + 1)
                if endpoint == '/api/logs':
                    self.assertEqual(fresh['summary']['backfilled_requests'], 2)
                    self.assertAlmostEqual(fresh['summary']['cost_computed_usd'], 0.002486)
                    self.assertAlmostEqual(fresh['summary']['cache_saved_usd'], 0.001854)

    def test_snapshot_transaction_is_released_on_query_error(self):
        original_read_conn = self.store.read_conn
        for endpoint, marker in (('/api/logs', 'SELECT id, ts, source'),
                                 ('/api/sessions', 'SELECT session_key, MIN(ts)')):
            with self.subTest(endpoint=endpoint):
                connections = []
                class Connection:
                    def __init__(self, conn):
                        self.conn = conn
                        connections.append(conn)
                    def execute(self, sql, *args):
                        if marker in sql:
                            raise RuntimeError('simulated query failure')
                        return self.conn.execute(sql, *args)
                    def __getattr__(self, name):
                        return getattr(self.conn, name)
                with patch.object(self.store, 'read_conn', side_effect=lambda: Connection(original_read_conn())):
                    with self.assertRaisesRegex(RuntimeError, 'simulated query failure'):
                        self.client.get(endpoint, params={'range': '1d', 'origin': 'all'})
                self.assertTrue(connections)
                self.assertFalse(connections[0].in_transaction)
                self.assertEqual(self.client.get(endpoint, params={'range': '1d', 'origin': 'all'}).status_code, 200)

    def test_export_is_filtered_capped_and_uses_one_snapshot(self):
        self.store.insert_usage([
            rec(ts=self.now - 10, session_key='export', cost_usd=0,
                raw={'request_id': f'export-{i}'}) for i in range(5001)
        ])
        original_read_conn = self.store.read_conn
        test = self
        inserted = []

        class Cursor:
            def __init__(self, cursor):
                self.cursor = cursor
            def fetchone(self):
                row = self.cursor.fetchone()
                if not inserted:
                    test.store.insert_usage([rec(ts=test.now, session_key='export',
                                                 raw={'request_id': 'inserted-during-export'})])
                    inserted.append(True)
                return row
            def __getattr__(self, name):
                return getattr(self.cursor, name)

        class Connection:
            def __init__(self, conn):
                self.conn = conn
            def execute(self, sql, *args):
                cursor = self.conn.execute(sql, *args)
                return Cursor(cursor) if 'SELECT COUNT(*) AS requests' in sql else cursor
            def __getattr__(self, name):
                return getattr(self.conn, name)

        params = {'range': '1d', 'origin': 'all', 'session_key': 'export'}
        with patch.object(self.store, 'read_conn', side_effect=lambda: Connection(original_read_conn())):
            response = self.client.get('/api/logs/export', params=params)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertTrue(inserted)
        self.assertEqual(data['total'], 5001)
        self.assertEqual(len(data['items']), 5000)
        self.assertNotIn('inserted-during-export', {row['trace_id'] for row in data['items']})
        fresh = self.client.get('/api/logs/export', params=params).json()
        self.assertEqual(fresh['total'], 5002)
        self.assertIn('inserted-during-export', {row['trace_id'] for row in fresh['items']})

    def test_model_series_uses_matching_day_buckets_for_all_time(self):
        response = self.client.get('/api/overview-usage', params={'range': 'all'})
        self.assertEqual(response.status_code, 200, response.text)
        snapshot = response.json()
        buckets = {row['bucket'] for row in snapshot['timeseries']}
        self.assertTrue(all(row['bucket'] in buckets for row in snapshot['model_timeseries']))
        self.assertEqual(sum(row['tokens'] for row in snapshot['model_timeseries']), 110)

    def test_hour_buckets_keep_distinct_elapsed_hours_across_dst(self):
        from app.api import stats

        zone = ZoneInfo('America/New_York')
        cases = [
            (datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc),
             [datetime(2026, 11, 1, hour, 30, tzinfo=timezone.utc) for hour in (5, 6)],
             ['2026-11-01 01:00', '2026-11-01 01:00'], ['-0400', '-0500']),
            (datetime(2026, 3, 8, 8, 30, tzinfo=timezone.utc),
             [datetime(2026, 3, 8, hour, 30, tzinfo=timezone.utc) for hour in (6, 7)],
             ['2026-03-08 01:00', '2026-03-08 03:00'], ['-0500', '-0400']),
        ]
        for end, records, labels, offsets in cases:
            with self.subTest(end=end):
                now = int(end.timestamp())
                self.store.insert_usage([
                    rec(ts=int(record.timestamp()), input_tokens=value)
                    for record, value in zip(records, (11, 23))
                ])
                with patch('app.api.stats.time.time', return_value=now), \
                     patch.object(stats, '_local_hour', side_effect=lambda ts: datetime.fromtimestamp(ts, zone)):
                    response = self.client.get('/api/overview-usage', params={'range': '1d'})
                self.assertEqual(response.status_code, 200, response.text)
                snapshot = response.json()
                series = snapshot['timeseries']
                self.assertEqual(len(series), 25)
                self.assertTrue(all(b['bucket_start'] - a['bucket_start'] == 3600
                                    for a, b in zip(series, series[1:])))
                populated = [row for row in series if row['requests']]
                self.assertEqual([row['bucket'] for row in populated], labels)
                self.assertEqual([row['bucket_offset'] for row in populated], offsets)
                self.assertEqual([row['input'] for row in populated], [11, 23])
                self.assertEqual(snapshot['summary']['total_tokens'], 34)
                by_start = {row['bucket_start']: row['tokens'] for row in snapshot['model_timeseries']}
                self.assertEqual([by_start[row['bucket_start']] for row in populated], [11, 23])

    def test_health_timeline_distinguishes_repeated_local_times(self):
        from app.api import stats

        zone = ZoneInfo('America/New_York')
        for end, hours, labels, offsets in [
            (datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc), (5, 6),
             ('01:30', '01:30'), ('-0400', '-0500')),
            (datetime(2026, 3, 8, 8, 30, tzinfo=timezone.utc), (6, 7),
             ('01:30', '03:30'), ('-0500', '-0400')),
        ]:
            with self.subTest(end=end):
                now = int(end.timestamp())
                with patch('app.api.stats.time.time', return_value=now), \
                     patch.object(stats, '_local_hour', side_effect=lambda ts: datetime.fromtimestamp(ts, zone)):
                    response = self.client.get('/api/health-timeline', params={'minutes': 180, 'bucket_sec': 1800})
                self.assertEqual(response.status_code, 200, response.text)
                by_ts = {row['ts']: row for row in response.json()['buckets']}
                rows = [by_ts[int(end.replace(hour=hour).timestamp())] for hour in hours]
                self.assertEqual(tuple(row['time'] for row in rows), labels)
                self.assertEqual(tuple(row['bucket_offset'] for row in rows), offsets)


if __name__ == '__main__':
    unittest.main()
