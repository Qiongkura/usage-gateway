"""New API 兼容中转站的余额采集器。"""
from __future__ import annotations

import time
from typing import Iterable

import httpx

from ..models import QuotaSnapshot
from .base import Collector

DEFAULT_QUOTA_PER_UNIT = 500_000


class RelayCollector(Collector):
    def __init__(self, name: str, base_url: str, access_token: str, user_id: str = ""):
        self.name = f"relay:{name}"
        self.base_url = base_url.rstrip("/").removesuffix("/v1")
        self.headers = {"Authorization": f"Bearer {access_token}"}
        if user_id:
            self.headers["New-Api-User"] = str(user_id)

    def collect_quota(self) -> Iterable[QuotaSnapshot]:
        now = int(time.time())
        unit = DEFAULT_QUOTA_PER_UNIT
        try:
            with httpx.Client(timeout=10) as client:
                try:
                    status = client.get(f"{self.base_url}/api/status", headers=self.headers).json()
                    unit = int((status.get("data") or {}).get("quota_per_unit") or unit)
                except Exception:
                    pass

                resp = client.get(f"{self.base_url}/api/user/self", headers=self.headers)
                resp.raise_for_status()
                data = resp.json().get("data") or {}
        except Exception:
            return []

        account = data.get("username") or data.get("display_name") or self.name
        quota = data.get("quota")
        used = data.get("used_quota")

        if quota is not None and quota != -1:
            yield QuotaSnapshot(
                ts=now, source=self.name, account=account, window="balance",
                remaining_amt=round(quota / unit, 6), currency="USD",
            )
        if used is not None:
            yield QuotaSnapshot(
                ts=now, source=self.name, account=account, window="all_time_spend",
                remaining_amt=round(used / unit, 6), currency="USD",
            )
