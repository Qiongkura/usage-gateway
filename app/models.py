"""统一数据模型。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class UsageRecord:
    """一条用量记录。"""

    ts: int
    source: str
    account: Optional[str] = None
    model: Optional[str] = None
    requested_model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    cost_usd: Optional[float] = None
    ttft_ms: Optional[float] = None
    latency_ms: Optional[float] = None
    status: int = 200
    client_key: Optional[str] = None
    raw: Any = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read + self.cache_write

    def dedupe_key(self) -> str:
        # 网关记录以 request_id 去重；本地采集以 source+时间+内容去重
        if isinstance(self.raw, dict) and self.raw.get("request_id"):
            return f"{self.source}|{self.raw['request_id']}"
        return (
            f"{self.source}|{self.ts}|{self.account}|{self.model}"
            f"|{self.input_tokens}|{self.output_tokens}|{self.cache_read}"
        )


@dataclass
class QuotaSnapshot:
    """一次额度/余额快照。"""

    ts: int
    source: str
    account: Optional[str] = None
    window: Optional[str] = None
    remaining_pct: Optional[float] = None
    remaining_amt: Optional[float] = None
    currency: Optional[str] = None
    resets_at: Optional[int] = None
    raw: Any = None


def dumps(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
