"""Guard the expected exclusions; this does not replace a Docker build test."""
import unittest
from pathlib import Path

import yaml


class DockerContextTests(unittest.TestCase):
    def test_sensitive_defaults_are_excluded(self):
        root = Path(__file__).resolve().parent.parent
        rules = {line.strip() for line in (root / ".dockerignore").read_text(encoding="utf-8").splitlines()}
        self.assertTrue({"data", "logs", "config.yaml", "*.key", "credentials*.json",
                         ".env", ".env.*", "model_health.json", "model_health.lock",
                         "model_probe_cooldown.json"} <= rules)
        self.assertNotIn("*.lock", rules, "uv.lock is required by the Dockerfile")

    def test_compose_is_local_only_by_default_and_uses_readiness(self):
        root = Path(__file__).resolve().parent.parent
        compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
        self.assertEqual(compose["services"]["gateway"]["ports"],
                         ["127.0.0.1:9000:9000"])
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("/api/ready", dockerfile)
        self.assertNotIn("/api/health", dockerfile)


if __name__ == "__main__":
    unittest.main()
