"""Mistral 官方 API 的额度采集器。

数据源：GET https://api.mistral.ai/v1/billing/budget

实际返回结构：
{
  "vibe_budget": {
    "usage_percentage": 3.53,
    "initial_budget": 150.0,
    "currency": "USD",
    "reset_at": "2026-12-01T00:00:00Z",
    "payg_enabled": false
  },
  "api_budget": { ... }
}
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Iterable

import httpx

from ..models import QuotaSnapshot
from .base import Collector


def _parse_iso(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


class MistralBillingCollector(Collector):
    def __init__(self, name: str = "mistral", base_url: str = "https://api.mistral.ai",
                 api_key: str = "", proxy: str | None = None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.proxy = proxy

    def collect_quota(self) -> Iterable[QuotaSnapshot]:
        if not self.api_key:
            return []

        kwargs: dict = {"timeout": 20, "trust_env": False}
        if self.proxy:
            kwargs["proxy"] = self.proxy

        try:
            with httpx.Client(**kwargs) as client:
                resp = client.get(
                    f"{self.base_url}/v1/billing/budget",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            return []

        if not isinstance(data, dict):
            return []

        now = int(time.time())
        for key, label in (("vibe_budget", "vibe"), ("api_budget", "api")):
            budget = data.get(key)
            if not isinstance(budget, dict):
                continue

            used_pct = _num(budget.get("usage_percentage"))
            initial = _num(budget.get("initial_budget"))
            currency = budget.get("currency") or "USD"
            resets_at = _parse_iso(budget.get("reset_at"))

            if used_pct is None:
                continue

            remaining_pct = round(max(0.0, 100.0 - used_pct), 2)
            remaining_amt = None
            if initial is not None:
                remaining_amt = round(initial * (1 - used_pct / 100.0), 4)

            yield QuotaSnapshot(
                ts=now,
                source=self.name,
                account=label,
                window=f"{label}_budget",
                remaining_pct=remaining_pct,
                remaining_amt=remaining_amt,
                currency=currency,
                resets_at=resets_at,
                raw=budget,
            )


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
