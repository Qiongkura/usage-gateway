"""Offline quota classification and API regression tests."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.alerts import QuotaAlerter, QuotaItem, item_level_name
from app.api.stats import router
from app.config import QuotaAlertConfig


class QuotaAlertAPITest(unittest.TestCase):
    def setUp(self):
        self.cfg = QuotaAlertConfig(warn_pct=60, critical_pct=20, stale_sec=123,
                                    amount_thresholds={"relay:axon": {"USD": {"warn": 10, "critical": 2}}})
        self.app = FastAPI()
        self.app.state.cfg = SimpleNamespace(alerts=self.cfg, stats_key="")
        self.app.state.store = object()
        self.app.state.alerter = QuotaAlerter(self.cfg)
        self.app.include_router(router)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def item(self, window, pct=None, amount=None, stale=False, currency="USD"):
        return QuotaItem(source="relay:axon", account="a", window=window,
                         remaining_pct=pct, remaining_amt=amount, currency=currency, stale=stale)

    def test_api_levels_counts_and_spend(self):
        items = [self.item("all_time_spend", amount=1), self.item("balance", amount=1),
                 self.item("fresh", pct=50), self.item("stale", pct=1, stale=True),
                 self.item("unknown", amount=1, currency="EUR"), self.item("ok", pct=90)]
        with patch("app.scheduler.load_quota_items", return_value=items) as load:
            quota = self.client.get("/api/quota").json()
            alerts = self.client.get("/api/alerts").json()
        self.assertEqual(load.call_args_list[0].args[1], 123)
        levels = {row["window"]: row["level"] for row in quota}
        self.assertEqual(levels, {"all_time_spend": "unknown", "balance": "critical",
                                  "fresh": "warn", "stale": "unknown",
                                  "unknown": "unknown", "ok": "ok"})
        self.assertEqual({row["window"]: row["level"] for row in alerts["items"]}, levels)
        self.assertEqual(alerts["counts"], {"critical": 1, "warn": 1, "ok": 1, "unknown": 3})
        events = self.app.state.alerter.evaluate(items, now=100)
        self.assertEqual({event.item.window for event in events}, {"balance", "fresh"})
        self.assertNotIn(items[0].state_key, self.app.state.alerter.states)

    def test_disabled_alerts_quota_uses_config(self):
        self.cfg.enabled = False
        self.app.state.alerter = None
        items = [self.item("percent", pct=50), self.item("balance", amount=5),
                 self.item("old", pct=1, stale=True)]
        with patch("app.scheduler.load_quota_items", return_value=items) as load:
            quota = self.client.get("/api/quota").json()
            alerts = self.client.get("/api/alerts").json()
        load.assert_called_once_with(self.app.state.store, 123)
        self.assertEqual([row["level"] for row in quota], ["warn", "warn", "unknown"])
        self.assertFalse(alerts["enabled"])

    def test_spend_and_unknown_do_not_change_state(self):
        alerter = self.app.state.alerter
        spend = self.item("all_time_spend", amount=1)
        for pct in (None, 1):
            spend.remaining_pct = pct
            self.assertEqual(item_level_name(spend, self.cfg), "unknown")
            self.assertEqual(alerter.evaluate([spend], now=100), [])
        balance = self.item("balance", amount=1)
        self.assertEqual(alerter.evaluate([balance], now=100)[0].kind, "critical")
        balance.stale = True
        balance.remaining_amt = 100
        self.assertEqual(alerter.evaluate([balance], now=200), [])
        balance.stale = False
        self.assertEqual(alerter.evaluate([balance], now=201)[0].kind, "recovered")


if __name__ == "__main__":
    unittest.main()
