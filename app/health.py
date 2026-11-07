"""上游健康状态与负载均衡。

职责：
  1. 记录每个上游的成功/失败
  2. 连续失败 N 次后进入冷却（cooldown），期间不参与路由
  3. 冷却结束后自动恢复
  4. 同一模型有多个上游时，按 priority + weight 选择
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Iterable

from .config import Upstream

log = logging.getLogger(__name__)


@dataclass
class HealthState:
    """单个上游的运行时健康状态。"""

    name: str
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    cooldown_until: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0
    last_ok_at: float = 0.0
    total_requests: int = 0
    total_failures: int = 0
    # 最近一次的延迟（毫秒）
    last_latency_ms: float = 0.0

    @property
    def in_cooldown(self) -> bool:
        return self.cooldown_until > time.time()

    @property
    def cooldown_remaining(self) -> float:
        return max(0.0, self.cooldown_until - time.time())

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "healthy": not self.in_cooldown,
            "in_cooldown": self.in_cooldown,
            "cooldown_remaining_sec": round(self.cooldown_remaining, 1),
            "consecutive_failures": self.consecutive_failures,
            "total_requests": self.total_requests,
            "total_failures": self.total_failures,
            "success_rate": (
                round((self.total_requests - self.total_failures) / self.total_requests * 100, 2)
                if self.total_requests else 100.0
            ),
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "last_ok_at": self.last_ok_at,
            "last_latency_ms": round(self.last_latency_ms, 1),
        }


class HealthTracker:
    """跟踪所有上游的健康状态。"""

    # 连续失败多少次进入冷却
    FAILURE_THRESHOLD = 3
    # 冷却时长（秒），逐次翻倍
    BASE_COOLDOWN = 30
    MAX_COOLDOWN = 600

    def __init__(self, upstreams: Iterable[Upstream]):
        self.states: dict[str, HealthState] = {
            up.name: HealthState(name=up.name) for up in upstreams
        }

    def get(self, name: str) -> HealthState:
        state = self.states.get(name)
        if state is None:
            state = HealthState(name=name)
            self.states[name] = state
        return state

    def record_success(self, name: str, latency_ms: float = 0.0) -> None:
        s = self.get(name)
        s.consecutive_failures = 0
        s.consecutive_successes += 1
        s.total_requests += 1
        s.last_ok_at = time.time()
        s.last_latency_ms = latency_ms
        if s.cooldown_until:
            log.info("[health] %s recovered from cooldown", name)
            s.cooldown_until = 0.0

    def record_failure(self, name: str, error: str) -> None:
        s = self.get(name)
        s.consecutive_failures += 1
        s.consecutive_successes = 0
        s.total_requests += 1
        s.total_failures += 1
        s.last_error = error[:300]
        s.last_error_at = time.time()

        if s.consecutive_failures >= self.FAILURE_THRESHOLD:
            # 指数退避：30s, 60s, 120s, 240s, 480s, 600s
            exponent = s.consecutive_failures - self.FAILURE_THRESHOLD
            cooldown = min(self.MAX_COOLDOWN, self.BASE_COOLDOWN * (2 ** exponent))
            s.cooldown_until = time.time() + cooldown
            log.warning(
                "[health] %s entered cooldown for %ds after %d consecutive failures: %s",
                name, cooldown, s.consecutive_failures, s.last_error[:100],
            )

    def is_available(self, name: str) -> bool:
        return not self.get(name).in_cooldown

    def snapshot(self) -> list[dict]:
        return [s.to_dict() for s in self.states.values()]


def choose_upstream(
    candidates: list[Upstream],
    tracker: HealthTracker,
    exclude: set[str] | None = None,
) -> Upstream | None:
    """从候选上游中选一个。

    规则：
      1. 排除冷却中的和 exclude 里的
      2. 按 priority 升序，只保留最小 priority 的那组
      3. 组内按 weight 加权随机
    """
    exclude = exclude or set()

    available = [
        up for up in candidates
        if up.name not in exclude and tracker.is_available(up.name)
    ]
    if not available:
        return None

    min_priority = min(up.priority for up in available)
    pool = [up for up in available if up.priority == min_priority]

    total_weight = sum(max(1, up.weight) for up in pool)
    if total_weight <= 0:
        return pool[0]

    pick = random.uniform(0, total_weight)
    acc = 0.0
    for up in pool:
        acc += max(1, up.weight)
        if pick <= acc:
            return up
    return pool[-1]


def candidates_for_model(
    model: str,
    upstreams: list[Upstream],
    explicit_prefix: str | None = None,
) -> list[Upstream]:
    """找出所有支持该模型的上游。

    如果指定了前缀，只返回那个上游（不参与负载均衡）。
    """
    if explicit_prefix:
        return [up for up in upstreams if up.prefix == explicit_prefix or up.name == explicit_prefix]
    return [up for up in upstreams if up.matches(model)]
