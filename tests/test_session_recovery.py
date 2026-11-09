"""Offline session attribution isolation and database/key recovery regressions."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Store  # noqa: E402
from app.models import UsageRecord  # noqa: E402
from app.session_attribution import KEY_FILENAME, SessionAttributor  # noqa: E402


class SessionRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    @staticmethod
    def fingerprint(attributor, client="client-a", source="header", session="private-session"):
        return attributor.fingerprint(session, client_id=client, source=source)

    def test_client_and_source_are_independently_isolated(self):
        attributor = SessionAttributor(self.root / "data")
        combinations = [(client, source) for client in ("client-a", "client-b")
                        for source in ("header", "other")]
        keys = [self.fingerprint(attributor, client, source) for client, source in combinations]
        self.assertEqual(len(set(keys)), len(combinations))
        self.assertTrue(all(isinstance(key, str) and len(key) == 64 for key in keys))
        self.assertEqual(keys[0], self.fingerprint(attributor))
        self.assertNotEqual(keys[0], self.fingerprint(attributor, session="another-session"))
        self.assertIsNone(self.fingerprint(attributor, session=None))
        # Length framing must distinguish otherwise ambiguous concatenations.
        self.assertNotEqual(self.fingerprint(attributor, "ab", "c"),
                            self.fingerprint(attributor, "a", "bc"))

    def test_hardlink_unsupported_volume_falls_back_to_rename(self):
        data = self.root / "fat32-like"
        # FAT32 and exFAT reject os.link with OSError(22).
        with patch("app.session_attribution.os.link",
                   side_effect=OSError(22, "unsupported")):
            first = SessionAttributor(data)
            first_key = self.fingerprint(first)
            self.assertEqual(len((data / KEY_FILENAME).read_bytes()), 32)
            self.assertEqual(self.fingerprint(SessionAttributor(data)), first_key)

    def test_fallback_does_not_overwrite_existing_key(self):
        data = self.root / "existing"
        original = SessionAttributor(data)
        original_key = self.fingerprint(original)
        with patch("app.session_attribution.os.link",
                   side_effect=OSError(22, "unsupported")):
            reopened = SessionAttributor(data)
        self.assertEqual(self.fingerprint(reopened), original_key)

    def test_persistent_key_is_shared_across_instances_but_not_directories(self):
        data = self.root / "data"
        first = SessionAttributor(data)
        key_bytes = (data / KEY_FILENAME).read_bytes()
        second = SessionAttributor(data)
        self.assertEqual(len(key_bytes), 32)
        self.assertEqual((data / KEY_FILENAME).read_bytes(), key_bytes)
        self.assertEqual(self.fingerprint(first), self.fingerprint(second))
        separate = SessionAttributor(self.root / "independent")
        self.assertNotEqual(self.fingerprint(first), self.fingerprint(separate))

    def test_database_and_key_backup_restore_and_missing_key(self):
        data, backup, restored = (self.root / name for name in ("data", "backup", "restored"))
        attributor = SessionAttributor(data)
        original_key = self.fingerprint(attributor)
        store = Store(data / "usage.db")
        try:
            record = UsageRecord(ts=123, source="provider", client_key="client-a",
                                 session_key=original_key, session_source="header",
                                 raw={"request_id": "original"})
            self.assertEqual(store.insert_usage([record]), 1)
            snapshot = store.backup_database(backup)
            self.assertIsNotNone(snapshot)
            self.assertTrue(snapshot.is_file())
            # The database snapshot alone does not include the attribution key.
            self.assertFalse((backup / KEY_FILENAME).exists())
            shutil.copy2(data / KEY_FILENAME, backup / KEY_FILENAME)
        finally:
            store.close()

        restored.mkdir()
        shutil.copy2(snapshot, restored / "usage.db")
        shutil.copy2(backup / KEY_FILENAME, restored / KEY_FILENAME)
        recovered = SessionAttributor(restored)
        self.assertEqual(self.fingerprint(recovered), original_key)
        recovered_store = Store(restored / "usage.db")
        try:
            row = recovered_store.conn.execute(
                "SELECT session_key, session_source FROM usage_records"
            ).fetchone()
            self.assertEqual(tuple(row), (original_key, "header"))
            self.assertEqual(recovered_store.insert_usage([UsageRecord(
                ts=124, source="provider", client_key="client-a", raw={"request_id": "after"},
                session_key=self.fingerprint(recovered), session_source="header")]), 1)
            self.assertEqual(recovered_store.conn.execute(
                "SELECT COUNT(*) FROM usage_records WHERE session_key = ?", (original_key,)
            ).fetchone()[0], 2)
        finally:
            recovered_store.close()

        # Losing the key creates a replacement; historic digests remain unchanged,
        # but new requests can no longer be correlated with them.
        (restored / KEY_FILENAME).unlink()
        replacement = SessionAttributor(restored)
        replacement_key = self.fingerprint(replacement)
        self.assertNotEqual(replacement_key, original_key)
        self.assertEqual(replacement_key, self.fingerprint(SessionAttributor(restored)))
        recovered_store = Store(restored / "usage.db")
        try:
            self.assertEqual(recovered_store.conn.execute(
                "SELECT COUNT(*) FROM usage_records WHERE session_key = ?", (original_key,)
            ).fetchone()[0], 2)
            self.assertEqual(recovered_store.conn.execute(
                "SELECT COUNT(*) FROM usage_records WHERE session_key = ?", (replacement_key,)
            ).fetchone()[0], 0)
        finally:
            recovered_store.close()
        # Restoring the original key restores correlation, without altering history.
        shutil.copy2(backup / KEY_FILENAME, restored / KEY_FILENAME)
        self.assertEqual(self.fingerprint(SessionAttributor(restored)), original_key)


if __name__ == "__main__":
    unittest.main()
