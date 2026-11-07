"""OpenCode Zen / Go 的额度采集器。

数据源：GET https://opencode.ai/inference/go/v1/usage

返回结构：
{
  "usage": {
    "rolling": {"status": "ok", "percent": 0,  "resetsAt": "..."},   # 5 小时窗口
    "weekly":  {"status": "ok", "percent": 40, "resetsAt": "..."},   # 周窗口
    "monthly": {"status": "ok", "percent": 47, "resetsAt": "..."}    # 月窗口
  }
}

percent 是"已用百分比"，这里转成"剩余百分比"。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Iterable

import httpx

from ..models import QuotaSnapshot
from .base import Collector

WINDOW_LABELS = {
    "rolling": ("5h", 300),
    "weekly": ("weekly", 10_080),
    "monthly": ("monthly", None),
}


def _parse_iso(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


class ZenUsageCollector(Collector):
    def __init__(self, name: str = "opencode", base_url: str = "https://opencode.ai",
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
                    f"{self.base_url}/inference/go/v1/usage",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "x-opencode-session": "usage-gateway-collector",
                        "user-agent": "usage-gateway/0.1.0",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            return []

        usage = (data or {}).get("usage") or {}
        if not isinstance(usage, dict):
            return []

        now = int(time.time())
        for key, (window, minutes) in WINDOW_LABELS.items():
            entry = usage.get(key)
            if not isinstance(entry, dict):
                continue

            status = entry.get("status")
            used_pct = entry.get("percent")
            if used_pct is None:
                continue

            try:
                used = float(used_pct)
            except (TypeError, ValueError):
                continue

            remaining = round(max(0.0, 100.0 - used), 2)
            raw = dict(entry)
            if minutes:
                raw["windowMinutes"] = minutes
            if status and status != "ok":
                raw["status"] = status

            yield QuotaSnapshot(
                ts=now,
                source=self.name,
                account="go",
                window=window,
                remaining_pct=remaining,
                resets_at=_parse_iso(entry.get("resetsAt")),
                raw=raw,
            )
