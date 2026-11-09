"""Measure the dashboard using synthetic data in a temporary SQLite database.

Run with: python tests/benchmark_dashboard.py [record-count]
This is a diagnostic benchmark, not a performance assertion or production forecast.
"""
from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from contextlib import ExitStack
from functools import wraps
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from test_logs_api import make_app
from app.api import stats as stats_api


def main() -> None:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
    if count < 1:
        raise ValueError("record-count must be positive")
    now = int(time.time())
    with tempfile.TemporaryDirectory(prefix="ug-dashboard-benchmark-") as tmp:
        app, store = make_app(Path(tmp))
        try:
            # 70% of records are spread across the past week; the rest across a year.
            rows = (
                (f"benchmark-{i}", now - ((i * 7919) % (7 * 86400 if i % 10 < 7 else 365 * 86400)),
                 f"source-{i % 4}", f"model-{(i // 4) % 24}", f"key-{(i // 96) % 10}",
                 100 + i % 101, 50 + i % 31, 30 + i % 41, 10 + i % 11,
                 0.001, 200, "ok", now)
                for i in range(count)
            )
            store.conn.executemany(
                "INSERT INTO usage_records (dedupe_key, ts, source, model, client_key, "
                "input_tokens, output_tokens, cache_read, cache_write, cost_usd, status, result, ingested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows,
            )
            store.conn.commit()
            for label, seconds in (('1d', 86400), ('7d', 7 * 86400), ('365d', 365 * 86400)):
                for access in ('', 'NOT INDEXED'):
                    sql = ("SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0), "
                           "COALESCE(SUM(cache_read), 0), COALESCE(SUM(cache_write), 0) "
                           f"FROM usage_records {access} WHERE ts BETWEEN ? AND ?")
                    params = (now - seconds, now)
                    plan = [row[3] for row in store.conn.execute('EXPLAIN QUERY PLAN ' + sql, params)]
                    samples = []
                    for _ in range(3):
                        start = time.perf_counter()
                        store.conn.execute(sql, params).fetchone()
                        samples.append(time.perf_counter() - start)
                    print(json.dumps({'range': label, 'access': access or 'planner', 'plan': plan,
                                      'scan_median_seconds': round(statistics.median(samples), 3)}))
            timings: dict[str, list[float]] = {}

            def timed(name, function):
                @wraps(function)
                def wrapper(*args, **kwargs):
                    start = time.perf_counter()
                    try:
                        return function(*args, **kwargs)
                    finally:
                        timings.setdefault(name, []).append(time.perf_counter() - start)
                return wrapper

            with ExitStack() as stack:
                client = stack.enter_context(TestClient(app))
                stack.enter_context(patch('app.api.stats.time.time', return_value=now))
                for name in ('_summary', '_timeseries', '_usage', '_active_models',
                             '_model_timeseries', '_costs', '_overview_model_breakdown', '_health_timeline'):
                    stack.enter_context(patch.object(stats_api, name, timed(name, getattr(stats_api, name))))
                print(f"records={count} models=24 sources=4 keys=10; 70% distributed over 7 days")
                for range_ in ('1d', '3d', '7d'):
                    timings.clear()
                    durations = []
                    for _ in range(3):
                        start = time.perf_counter()
                        response = client.get('/api/overview-usage', params={'range': range_})
                        durations.append(time.perf_counter() - start)
                        response.raise_for_status()
                    data = response.json()
                    summary = data['summary']
                    totals = {
                        'models': sum(row['tokens'] for row in data['models']),
                        'model_series': sum(row['tokens'] for row in data['model_timeseries']),
                        'timeseries': sum(sum(row[k] for k in ('input', 'output', 'cache_read', 'cache_write'))
                                          for row in data['timeseries']),
                        'health': sum(row['tokens'] for row in data['health_timeline']['buckets']),
                    }
                    assert all(value == summary['total_tokens'] for value in totals.values()), (range_, totals, summary)
                    assert sum(row['requests'] for row in data['models']) == summary['total_requests']
                    assert data['health_timeline']['total_requests'] == summary['total_requests']
                    print(json.dumps({
                        'range': range_, 'requests': summary['total_requests'],
                        'tokens': summary['total_tokens'], 'bytes': len(response.content),
                        'seconds': [round(value, 3) for value in durations],
                        'median_seconds': round(statistics.median(durations), 3),
                        'model_series_rows': len(data['model_timeseries']), 'totals_match': True,
                        'stage_median_seconds': {name: round(statistics.median(values), 3)
                                                 for name, values in timings.items()},
                    }))
        finally:
            store.close()


if __name__ == '__main__':
    main()
