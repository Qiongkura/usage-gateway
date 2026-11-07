"""请求链路追踪。

记录一个请求经过的每个阶段：
  1. 收到请求
  2. 鉴权
  3. 路由选择
  4. 上游请求
  5. 上游响应
  6. 用量解析
  7. 落库

用于排查「为什么慢」「为什么失败」。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TraceSpan:
    """一个阶段。"""

    name: str
    start_ms: float
    end_ms: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def duration_ms(self) -> float | None:
        if self.end_ms is None:
            return None
        return round(self.end_ms - self.start_ms, 2)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "start_ms": round(self.start_ms, 2),
            "end_ms": round(self.end_ms, 2) if self.end_ms is not None else None,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
            "error": self.error,
        }


class Trace:
    """一次请求的完整追踪。"""

    def __init__(self, request_id: str | None = None):
        self.id = request_id or uuid.uuid4().hex
        self.t0 = time.perf_counter() * 1000
        self.spans: list[TraceSpan] = []
        self._current: TraceSpan | None = None

    def _now(self) -> float:
        return round(time.perf_counter() * 1000 - self.t0, 2)

    def start(self, name: str, **detail) -> "Trace":
        self.end_current()
        self._current = TraceSpan(name=name, start_ms=self._now(), detail=detail)
        self.spans.append(self._current)
        return self

    def end_current(self, error: str | None = None) -> None:
        if self._current is not None and self._current.end_ms is None:
            self._current.end_ms = self._now()
            if error:
                self._current.error = error

    def add(self, name: str, duration_ms: float, **detail) -> "Trace":
        """直接添加一个已完成的阶段。"""
        now = self._now()
        self.spans.append(TraceSpan(
            name=name,
            start_ms=round(now - duration_ms, 2),
            end_ms=now,
            detail=detail,
        ))
        return self

    def finish(self) -> dict:
        self.end_current()
        total = self._now()
        return {
            "trace_id": self.id,
            "total_ms": round(total, 2),
            "spans": [s.to_dict() for s in self.spans],
        }


def summarize(trace: dict) -> str:
    """把 trace 压成一行，方便日志。"""
    parts = []
    for s in trace.get("spans", []):
        d = s.get("duration_ms")
        if d is None:
            continue
        parts.append(f"{s['name']}={d:.0f}ms")
    return " ".join(parts)
