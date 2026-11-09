"""Request persistence must roll back under the same write lock."""
import tempfile
import threading
import unittest
import sqlite3
from pathlib import Path

from app.db import Store
from app.models import QuotaSnapshot, UsageRecord


class RequestAtomicityTest(unittest.TestCase):
    def test_failed_trace_insert_rolls_back_before_unlock(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "usage.db")
            store.conn.execute("CREATE TRIGGER reject_trace BEFORE INSERT ON request_traces BEGIN SELECT RAISE(ABORT, 'fail'); END")
            store.conn.commit()
            original = store.lock
            class ObservedLock:
                def __enter__(self):
                    original.acquire()
                    return self
                def __exit__(self, *args):
                    # No unfinished transaction may escape the critical section.
                    self_in_transaction.append(store.conn.in_transaction)
                    original.release()
            self_in_transaction = []
            store.lock = ObservedLock()
            record = UsageRecord(ts=1, source="test", raw={"request_id": "failed"})
            with self.assertLogs("usage-gateway.db", level="ERROR"):
                store.record_request(usage_record=record, trace={"trace_id": "failed"}, ts=1)
            self.assertEqual(self_in_transaction, [False])
            self.assertEqual(store.conn.execute("SELECT count(*) FROM usage_records").fetchone()[0], 0)
            store.lock = original
            store.conn.execute("DROP TRIGGER reject_trace")
            store.conn.commit()
            def write():
                store.record_request(usage_record=UsageRecord(ts=2, source="test", raw={"request_id": "ok"}), ts=2)
            thread = threading.Thread(target=write)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(store.conn.execute("SELECT count(*) FROM usage_records").fetchone()[0], 1)
            store.close()

    def test_failed_batches_roll_back_before_unlock(self):
        for kind in ("usage", "quota"):
            for failure in ("trigger", "iterator"):
                with self.subTest(kind=kind, failure=failure), tempfile.TemporaryDirectory() as tmp:
                    store = Store(Path(tmp) / "usage.db")
                    try:
                        table = "usage_records" if kind == "usage" else "quota_snapshots"
                        make_record = (lambda ts: UsageRecord(ts=ts, source="test")) if kind == "usage" else (
                            lambda ts: QuotaSnapshot(ts=ts, source="test")
                        )
                        insert = store.insert_usage if kind == "usage" else store.insert_quota
                        if failure == "trigger":
                            store.conn.execute(
                                f"CREATE TRIGGER reject_second BEFORE INSERT ON {table} "
                                "WHEN NEW.ts = 2 BEGIN SELECT RAISE(ABORT, 'fail'); END"
                            )
                            store.conn.commit()

                        def records():
                            yield make_record(1)
                            if failure == "iterator":
                                raise RuntimeError("iterator failed")
                            yield make_record(2)

                        original = store.lock
                        states = []

                        class ObservedLock:
                            def __enter__(self):
                                original.acquire()
                                return self

                            def __exit__(self, *args):
                                states.append(store.conn.in_transaction)
                                original.release()

                        store.lock = ObservedLock()
                        error = sqlite3.IntegrityError if failure == "trigger" else RuntimeError
                        with self.assertRaises(error):
                            insert(records())
                        self.assertEqual(states, [False])
                        self.assertEqual(store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
                        store.lock = original
                        self.assertEqual(insert([make_record(3)]), 1)
                        self.assertEqual(
                            [row[0] for row in store.conn.execute(f"SELECT ts FROM {table}")], [3]
                        )
                    finally:
                        store.close()
