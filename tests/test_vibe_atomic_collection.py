import json
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.collectors.vibe import VibeCollector
from app.db import CollectorStateConflict, Store
from app.scheduler import Scheduler


def write_meta(home, sid, prompt, cached, output, ts):
    path = home / "logs" / "session" / sid / "meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"metadata": {
        "session_prompt_tokens": prompt,
        "session_cached_tokens": cached,
        "session_completion_tokens": output,
    }}), encoding="utf-8")
    os.utime(path, (ts, ts))


def run_paused_vibe_collection(db_path, home, prepared, resume, results):
    store = Store(Path(db_path))
    try:
        collector = VibeCollector(home)
        scheduler = Scheduler(store, [collector], probe_days=0)
        prepare = collector.prepare_usage
        calls = 0

        def pause_first(since, cursor):
            nonlocal calls
            calls += 1
            result = prepare(since, cursor)
            if calls == 1:
                prepared.set()
                if not resume.wait(15):
                    raise TimeoutError("second collector did not finish")
            return result

        with patch.object(collector, "prepare_usage", side_effect=pause_first):
            scheduler._run_usage(collector)
        results.put(("ok", calls))
    except BaseException as exc:
        results.put(("error", repr(exc)))
        raise
    finally:
        store.close()


def run_vibe_collection(db_path, home, results):
    store = Store(Path(db_path))
    try:
        collector = VibeCollector(home)
        Scheduler(store, [collector], probe_days=0)._run_usage(collector)
        results.put(("ok", None))
    except BaseException as exc:
        results.put(("error", repr(exc)))
        raise
    finally:
        store.close()


class VibeAtomicCollectionTest(unittest.TestCase):
    def test_missing_session_root_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = VibeCollector(Path(tmp) / "vibe")
            records, cursor = collector.prepare_usage(0, None)
            self.assertEqual(records, [])
            self.assertEqual(json.loads(cursor), {})

    def test_missing_metadata_is_retried_after_watermark_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "vibe"
            (home / "logs" / "session" / "late").mkdir(parents=True)
            write_meta(home, "newer", 100, 20, 10, 20)
            store = Store(root / "usage.db")
            try:
                collector = VibeCollector(home)
                scheduler = Scheduler(store, [collector], probe_days=0)
                scheduler._run_usage(collector)
                self.assertEqual(store.watermark("vibe"), 20)
                self.assertEqual(json.loads(store.collector_cursor("vibe"))["late"], {"pending": True})
                write_meta(home, "late", 50, 10, 5, 10)
                scheduler._run_usage(collector)
                scheduler._run_usage(collector)
                rows = store.conn.execute(
                    "SELECT account, input_tokens, cache_read, output_tokens FROM usage_records"
                ).fetchall()
                self.assertCountEqual([tuple(row) for row in rows],
                                      [("newer", 80, 20, 10), ("late", 40, 10, 5)])
            finally:
                store.close()

    def test_new_session_with_unreadable_mtime_is_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "vibe"
            write_meta(home, "late", 50, 10, 5, 10)
            write_meta(home, "newer", 100, 20, 10, 20)
            late = home / "logs" / "session" / "late" / "meta.json"
            store = Store(root / "usage.db")
            try:
                collector = VibeCollector(home)
                scheduler = Scheduler(store, [collector], probe_days=0)
                stat = Path.stat

                def fail_late(path, *args, **kwargs):
                    if path == late:
                        raise OSError("temporarily unavailable")
                    return stat(path, *args, **kwargs)

                with patch.object(Path, "stat", fail_late):
                    scheduler._run_usage(collector)
                self.assertEqual(store.watermark("vibe"), 20)
                self.assertEqual(json.loads(store.collector_cursor("vibe"))["late"], {"pending": True})
                scheduler._run_usage(collector)
                scheduler._run_usage(collector)
                rows = store.conn.execute(
                    "SELECT account, input_tokens, cache_read, output_tokens FROM usage_records"
                ).fetchall()
                self.assertCountEqual([tuple(row) for row in rows],
                                      [("newer", 80, 20, 10), ("late", 40, 10, 5)])
            finally:
                store.close()

    def test_directory_enumeration_failure_does_not_commit_partial_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "vibe"
            write_meta(home, "late", 50, 10, 5, 10)
            write_meta(home, "newer", 100, 20, 10, 20)
            store = Store(root / "usage.db")
            try:
                collector = VibeCollector(home)
                scheduler = Scheduler(store, [collector], probe_days=0)
                scandir = os.scandir

                class InterruptedScan:
                    def __enter__(self):
                        self.iterator = scandir(home / "logs" / "session")
                        self.entries = iter(self.iterator)
                        return self

                    def __exit__(self, *args):
                        self.iterator.close()

                    def __iter__(self):
                        yield next(self.entries)
                        raise PermissionError("directory became unreadable")

                with patch("app.collectors.vibe.os.scandir", return_value=InterruptedScan()):
                    with self.assertRaises(PermissionError):
                        scheduler._run_usage(collector)
                self.assertEqual(store.collector_position("vibe"), (0, None))
                self.assertEqual(store.conn.execute("SELECT count(*) FROM usage_records").fetchone()[0], 0)
                scheduler._run_usage(collector)
                scheduler._run_usage(collector)
                rows = store.conn.execute(
                    "SELECT account, input_tokens, cache_read, output_tokens FROM usage_records"
                ).fetchall()
                self.assertCountEqual([tuple(row) for row in rows],
                                      [("newer", 80, 20, 10), ("late", 40, 10, 5)])
            finally:
                store.close()

    def test_entry_type_failure_does_not_commit_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "vibe"
            write_meta(home, "s", 50, 10, 5, 10)
            store = Store(root / "usage.db")
            try:
                collector = VibeCollector(home)
                scheduler = Scheduler(store, [collector], probe_days=0)

                class UnreadableEntry:
                    def is_dir(self):
                        raise PermissionError("entry type unavailable")

                class UnreadableScan:
                    def __enter__(self):
                        return iter([UnreadableEntry()])

                    def __exit__(self, *args):
                        return False

                with patch("app.collectors.vibe.os.scandir", return_value=UnreadableScan()):
                    with self.assertRaises(PermissionError):
                        scheduler._run_usage(collector)
                self.assertEqual(store.collector_position("vibe"), (0, None))
                self.assertEqual(store.conn.execute("SELECT count(*) FROM usage_records").fetchone()[0], 0)
                scheduler._run_usage(collector)
                self.assertEqual(store.conn.execute("SELECT count(*) FROM usage_records").fetchone()[0], 1)
            finally:
                store.close()

    def test_initial_zero_session_is_tracked_after_watermark_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "vibe"
            write_meta(home, "zero", 0, 0, 0, 10)
            write_meta(home, "newer", 100, 20, 10, 20)
            store = Store(root / "usage.db")
            try:
                scheduler = Scheduler(store, [VibeCollector(home)], probe_days=0)
                scheduler._run_usage(VibeCollector(home))
                self.assertEqual(json.loads(store.collector_cursor("vibe"))["zero"]["t"], 0)
                write_meta(home, "zero", 30, 5, 2, 10)
                scheduler._run_usage(VibeCollector(home))
                rows = store.conn.execute(
                    "SELECT account, input_tokens, cache_read, output_tokens FROM usage_records"
                ).fetchall()
                self.assertCountEqual([tuple(row) for row in rows],
                                      [("newer", 80, 20, 10), ("zero", 25, 5, 2)])
            finally:
                store.close()

    def test_invalid_new_session_is_retried_after_watermark_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "vibe"
            path = home / "logs" / "session" / "late" / "meta.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"stats":', encoding="utf-8")
            os.utime(path, (10, 10))
            write_meta(home, "newer", 100, 20, 10, 20)
            store = Store(root / "usage.db")
            try:
                scheduler = Scheduler(store, [VibeCollector(home)], probe_days=0)
                scheduler._run_usage(VibeCollector(home))
                self.assertEqual(store.watermark("vibe"), 20)
                self.assertEqual(json.loads(store.collector_cursor("vibe"))["late"], {"pending": True})
                write_meta(home, "late", 50, 10, 5, 10)
                scheduler._run_usage(VibeCollector(home))
                scheduler._run_usage(VibeCollector(home))
                rows = store.conn.execute(
                    "SELECT account, input_tokens, cache_read, output_tokens FROM usage_records"
                ).fetchall()
                self.assertCountEqual([tuple(row) for row in rows],
                                      [("newer", 80, 20, 10), ("late", 40, 10, 5)])
                self.assertEqual(store.watermark("vibe"), 20)
            finally:
                store.close()

    def test_pending_session_with_zero_counters_remains_tracked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "vibe"
            path = home / "logs" / "session" / "late" / "meta.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"stats":', encoding="utf-8")
            os.utime(path, (10, 10))
            write_meta(home, "newer", 100, 20, 10, 20)
            store = Store(root / "usage.db")
            try:
                scheduler = Scheduler(store, [VibeCollector(home)], probe_days=0)
                scheduler._run_usage(VibeCollector(home))
                write_meta(home, "late", 0, 0, 0, 10)
                scheduler._run_usage(VibeCollector(home))
                self.assertEqual(json.loads(store.collector_cursor("vibe"))["late"]["t"], 0)
                write_meta(home, "late", 30, 5, 2, 10)
                scheduler._run_usage(VibeCollector(home))
                rows = store.conn.execute(
                    "SELECT account, input_tokens, cache_read, output_tokens FROM usage_records"
                ).fetchall()
                self.assertCountEqual([tuple(row) for row in rows],
                                      [("newer", 80, 20, 10), ("late", 25, 5, 2)])
            finally:
                store.close()

    def test_top_level_stats_are_collected_incrementally(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "vibe"
            path = home / "logs" / "session" / "s" / "meta.json"
            path.parent.mkdir(parents=True)
            store = Store(root / "usage.db")
            try:
                collector = VibeCollector(home)
                scheduler = Scheduler(store, [collector], probe_days=0)
                for timestamp, prompt, cached, output in ((10, 100, 20, 10), (11, 150, 30, 15)):
                    path.write_text(json.dumps({
                        "metadata": {"unrelated": "value"},
                        "stats": {"session_prompt_tokens": prompt,
                                  "session_cached_tokens": cached,
                                  "session_completion_tokens": output},
                    }), encoding="utf-8")
                    os.utime(path, (timestamp, timestamp))
                    scheduler._run_usage(collector)
                scheduler._run_usage(VibeCollector(home))
                rows = store.conn.execute(
                    "SELECT input_tokens, cache_read, output_tokens FROM usage_records ORDER BY id"
                ).fetchall()
                self.assertEqual([tuple(row) for row in rows], [(80, 20, 10), (40, 10, 5)])
            finally:
                store.close()

    def test_delayed_cache_counter_warns_about_possible_overstatement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "vibe"
            store = Store(root / "usage.db")
            try:
                collector = VibeCollector(home)
                scheduler = Scheduler(store, [collector], probe_days=0)
                write_meta(home, "s", 100, 0, 0, 10)
                scheduler._run_usage(collector)
                write_meta(home, "s", 100, 80, 0, 11)
                with self.assertLogs("app.collectors.vibe", level="WARNING") as captured:
                    scheduler._run_usage(collector)
                self.assertIn("may be overstated", captured.output[0])
                rows = store.conn.execute(
                    "SELECT input_tokens, cache_read FROM usage_records ORDER BY id"
                ).fetchall()
                self.assertEqual([tuple(row) for row in rows], [(100, 0), (0, 80)])
            finally:
                store.close()

    def test_competing_processes_retry_from_committed_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path, home = root / "usage.db", root / "vibe"
            store = Store(db_path)
            store.close()
            write_meta(home, "s", 100, 20, 10, 10)
            ctx = multiprocessing.get_context("spawn")
            prepared, resume = ctx.Event(), ctx.Event()
            old_results, new_results = ctx.Queue(), ctx.Queue()
            old = ctx.Process(target=run_paused_vibe_collection,
                              args=(str(db_path), str(home), prepared, resume, old_results))
            new = None
            try:
                old.start()
                self.assertTrue(prepared.wait(15), "old collector did not prepare")
                write_meta(home, "s", 150, 30, 15, 11)
                new = ctx.Process(target=run_vibe_collection,
                                  args=(str(db_path), str(home), new_results))
                new.start()
                new.join(15)
                self.assertFalse(new.is_alive(), "new collector did not finish")
                self.assertEqual(new.exitcode, 0)
                self.assertEqual(new_results.get(timeout=5), ("ok", None))
                resume.set()
                old.join(15)
                self.assertFalse(old.is_alive(), "old collector did not finish")
                self.assertEqual(old.exitcode, 0)
                self.assertEqual(old_results.get(timeout=5), ("ok", 2))
                store = Store(db_path)
                try:
                    rows = store.conn.execute(
                        "SELECT input_tokens, cache_read, output_tokens FROM usage_records"
                    ).fetchall()
                    self.assertEqual([tuple(row) for row in rows], [(120, 30, 15)])
                    self.assertEqual(json.loads(store.collector_cursor("vibe"))["s"]["t"], 150)
                    write_meta(home, "s", 170, 30, 20, 12)
                    collector = VibeCollector(home)
                    Scheduler(store, [collector], probe_days=0)._run_usage(collector)
                    rows = store.conn.execute(
                        "SELECT input_tokens, cache_read, output_tokens FROM usage_records ORDER BY id"
                    ).fetchall()
                    self.assertEqual([tuple(row) for row in rows], [(120, 30, 15), (20, 0, 5)])
                finally:
                    store.close()
            finally:
                resume.set()
                for process in (old, new):
                    if process is not None and process.is_alive():
                        process.terminate()
                        process.join(5)
                old_results.close()
                new_results.close()

    def test_repeated_conflicts_stop_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "vibe"
            write_meta(home, "s", 100, 20, 10, 10)
            store = Store(root / "usage.db")
            try:
                collector = VibeCollector(home)
                scheduler = Scheduler(store, [collector], probe_days=0)
                with patch.object(store, "insert_usage", side_effect=CollectorStateConflict("vibe")) as insert:
                    with self.assertRaises(CollectorStateConflict):
                        scheduler._run_usage(collector)
                    self.assertEqual(insert.call_count, 3)
                self.assertEqual(store.collector_position("vibe"), (0, None))
                self.assertEqual(store.conn.execute("SELECT count(*) FROM usage_records").fetchone()[0], 0)
            finally:
                store.close()

    def test_competing_collectors_retry_from_committed_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "vibe"
            first = Store(root / "usage.db")
            second = Store(root / "usage.db")
            try:
                collector = VibeCollector(home)
                first_scheduler = Scheduler(first, [collector], probe_days=0)
                second_collector = VibeCollector(home)
                second_scheduler = Scheduler(second, [second_collector], probe_days=0)
                write_meta(home, "s", 100, 20, 10, 10)
                prepare = collector.prepare_usage

                def interleave(since, cursor):
                    result = prepare(since, cursor)
                    write_meta(home, "s", 150, 30, 15, 11)
                    second_scheduler._run_usage(second_collector)
                    return result

                with patch.object(collector, "prepare_usage") as mocked:
                    mocked.side_effect = lambda since, cursor: (
                        interleave(since, cursor) if mocked.call_count == 1 else prepare(since, cursor)
                    )
                    first_scheduler._run_usage(collector)
                rows = first.conn.execute(
                    "SELECT input_tokens, cache_read, output_tokens FROM usage_records"
                ).fetchall()
                self.assertEqual([tuple(row) for row in rows], [(120, 30, 15)])
                self.assertEqual(json.loads(first.collector_cursor("vibe"))["s"]["t"], 150)
                write_meta(home, "s", 170, 30, 20, 12)
                first_scheduler._run_usage(collector)
                rows = first.conn.execute(
                    "SELECT input_tokens, cache_read, output_tokens FROM usage_records ORDER BY id"
                ).fetchall()
                self.assertEqual([tuple(row) for row in rows], [(120, 30, 15), (20, 0, 5)])
            finally:
                first.close()
                second.close()

    def test_reset_snapshot_is_persisted_and_new_cycle_is_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "vibe"
            store = Store(root / "usage.db")
            try:
                collector = VibeCollector(home)
                scheduler = Scheduler(store, [collector], probe_days=0)
                write_meta(home, "s", 100, 20, 10, 10)
                scheduler._run_usage(collector)
                old_cursor = store.collector_cursor("vibe")
                path = home / "logs" / "session" / "s" / "meta.json"
                path.write_text(json.dumps({"metadata": {"session_prompt_tokens": None}}), encoding="utf-8")
                os.utime(path, (11, 11))
                scheduler._run_usage(collector)
                self.assertEqual(store.collector_cursor("vibe"), old_cursor)
                write_meta(home, "s", 0, 0, 0, 11)
                store.conn.execute("CREATE TRIGGER reject_cursor BEFORE UPDATE ON collector_state "
                                   "BEGIN SELECT RAISE(ABORT, 'fail'); END")
                store.conn.commit()
                with self.assertRaises(sqlite3.IntegrityError):
                    scheduler._run_usage(collector)
                self.assertEqual(store.collector_cursor("vibe"), old_cursor)
                store.conn.execute("DROP TRIGGER reject_cursor")
                store.conn.commit()
                scheduler._run_usage(collector)
                self.assertEqual(json.loads(store.collector_cursor("vibe"))["s"]["t"], 0)
                self.assertEqual(store.conn.execute("SELECT count(*) FROM usage_records").fetchone()[0], 1)
                write_meta(home, "s", 30, 10, 5, 12)
                scheduler._run_usage(VibeCollector(home))
                write_meta(home, "s", 10, 2, 3, 13)
                scheduler._run_usage(collector)
                rows = store.conn.execute(
                    "SELECT input_tokens, cache_read, output_tokens FROM usage_records ORDER BY id"
                ).fetchall()
                self.assertEqual([tuple(row) for row in rows], [(80, 20, 10), (20, 10, 5), (8, 2, 3)])
            finally:
                store.close()

    def test_failure_retry_restart_and_late_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home, state = root / "vibe", root / "vibe_state.json"
            state.write_text(json.dumps({"old": {"t": 100, "c": 20, "o": 10, "mtime": 10}}))
            write_meta(home, "old", 130, 20, 15, 20)
            store = Store(root / "usage.db")
            try:
                store.set_watermark("vibe", 10)
                collector = VibeCollector(home, state)
                scheduler = Scheduler(store, [collector], probe_days=0)
                store.conn.execute("CREATE TRIGGER reject_cursor BEFORE UPDATE ON collector_state "
                                   "BEGIN SELECT RAISE(ABORT, 'fail'); END")
                store.conn.commit()
                with self.assertRaises(sqlite3.IntegrityError):
                    scheduler._run_usage(collector)
                self.assertEqual(store.watermark("vibe"), 10)
                self.assertIsNone(store.collector_cursor("vibe"))
                self.assertEqual(store.conn.execute("SELECT count(*) FROM usage_records").fetchone()[0], 0)
                store.conn.execute("DROP TRIGGER reject_cursor")
                store.conn.commit()
                scheduler._run_usage(collector)
                self.assertEqual(store.watermark("vibe"), 20)
                self.assertEqual(store.conn.execute("SELECT input_tokens FROM usage_records").fetchone()[0], 30)

                collector = VibeCollector(home, state)
                scheduler._run_usage(collector)
                self.assertEqual(store.conn.execute("SELECT count(*) FROM usage_records").fetchone()[0], 1)
                write_meta(home, "old", 150, 20, 20, 20)
                write_meta(home, "late", 70, 10, 5, 20)
                scheduler._run_usage(collector)
                rows = store.conn.execute("SELECT account, input_tokens FROM usage_records ORDER BY id").fetchall()
                self.assertCountEqual([(row[0], row[1]) for row in rows], [("old", 30), ("old", 20), ("late", 60)])
                scheduler._run_usage(VibeCollector(home, state))
                self.assertEqual(store.conn.execute("SELECT count(*) FROM usage_records").fetchone()[0], 3)
                write_meta(home, "old", 170, 20, 25, 20)
                scheduler._run_usage(collector)
                self.assertEqual(store.conn.execute("SELECT count(*) FROM usage_records").fetchone()[0], 4)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
