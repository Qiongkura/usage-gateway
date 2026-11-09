import tempfile
import unittest
from pathlib import Path

import yaml

from app.config import load_config


class ClientNameUniquenessTests(unittest.TestCase):
    def load_clients(self, clients):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(yaml.safe_dump({"gateway": {"client_keys": clients}}), encoding="utf-8")
            return load_config(path).client_keys

    def test_unique_names_and_existing_defaults(self):
        clients = self.load_clients([
            {"name": "alice", "key": "one"},
            {"key": "two"},
            {"name": "", "key": "three"},
            {"name": "Alice", "key": "four"},
            {"name": " alice ", "key": "five"},
            {"name": "alice"},  # An entry without a key is ignored.
        ])
        self.assertEqual([c.name for c in clients], ["alice", "key-1", "key-2", "Alice", " alice "])

    def test_duplicate_names_rejected_without_disclosing_secrets(self):
        secret_name = "sensitive-client-name"
        secrets = (secret_name, "sensitive-key-one", "sensitive-key-two")
        with self.assertRaises(ValueError) as caught:
            self.load_clients([
                {"name": secret_name, "key": secrets[1]},
                {"name": secret_name, "key": secrets[2]},
            ])
        self.assertIn("gateway.client_keys", str(caught.exception))
        for secret in secrets:
            self.assertNotIn(secret, str(caught.exception))

    def test_explicit_name_collides_with_generated_name(self):
        with self.assertRaises(ValueError):
            self.load_clients([{"name": "key-1", "key": "one"}, {"key": "two"}])

    def test_names_are_checked_after_string_conversion(self):
        with self.assertRaises(ValueError):
            self.load_clients([{"name": 123, "key": "one"}, {"name": "123", "key": "two"}])

    def test_duplicate_name_rejected_even_for_same_key(self):
        with self.assertRaises(ValueError):
            self.load_clients([{"name": "same", "key": "one"}, {"name": "same", "key": "one"}])


if __name__ == "__main__":
    unittest.main()
