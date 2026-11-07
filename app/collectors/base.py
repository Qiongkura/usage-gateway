"""采集器基类。"""
from __future__ import annotations

from typing import Iterable

from ..models import QuotaSnapshot, UsageRecord


class Collector:
    name: str = "base"

    def collect_usage(self, since_ts: int) -> Iterable[UsageRecord]:
        return []

    def collect_quota(self) -> Iterable[QuotaSnapshot]:
        return []
