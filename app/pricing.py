"""价格表：按上游 × 模型定价。

三种来源：
  1. Zen 的 /v1/models 直接带 cost 字段（含缓存价和时段倍率）→ 自动拉取
  2. Mistral 官方定价 → 手工配置
  3. Antigravity / 中转站 → 手工配置

价格单位：USD / 1M tokens
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _model_candidates(model: str) -> list[str]:
    """生成模型名的所有可能写法，按优先级从精确到宽松。

    "google/gemma-4-31b-it"  ->  ["google/gemma-4-31b-it", "gemma-4-31b-it"]
    "gemini-3.8-flash-tiered" ->  [..., "gemini-3.8-flash"]
    "mistral-small"          ->  [..., "mistral-small-latest"]
    """
    out: list[str] = []

    def add(name: str) -> None:
        if name and name not in out:
            out.append(name)

    add(model)

    # 1. 去掉厂商前缀：google/xxx、openai/xxx、anthropic/xxx
    base = model
    if "/" in model:
        base = model.split("/", 1)[1]
        add(base)

    # 2. 去掉变体后缀
    suffixes = ("-tiered", "-high", "-low", "-medium", "-thinking", "-preview", "-latest")
    for suffix in suffixes:
        for cur in list(out):
            if cur.endswith(suffix):
                add(cur[: -len(suffix)])

    # 3. 补上 -latest
    for cur in list(out):
        add(f"{cur}-latest")

    # 4. 去掉版本后缀：claude-sonnet-4-6 -> claude-sonnet
    for cur in list(out):
        parts = cur.rsplit("-", 2)
        if len(parts) == 3 and parts[-1].isdigit() and parts[-2].isdigit():
            add(parts[0])

    return out


@dataclass
class ModelPrice:
    input: float = 0.0          # USD / 1M input tokens
    output: float = 0.0         # USD / 1M output tokens
    cache_read: float = 0.0     # USD / 1M cached tokens
    cache_write: float = 0.0    # USD / 1M cache write tokens
    # 时段倍率：[{"when": {...}, "factor": 2.0}]
    multipliers: list[dict] = field(default_factory=list)


@dataclass
class PriceTable:
    """按 upstream name -> model -> ModelPrice 组织。"""

    table: dict[str, dict[str, ModelPrice]] = field(default_factory=dict)

    def get(self, upstream: str, model: str | None) -> ModelPrice | None:
        if not model:
            return None
        prices = self.table.get(upstream) or {}
        if not prices:
            return None

        # 依次尝试多种写法，第一个命中的胜出
        for candidate in _model_candidates(model):
            if candidate in prices:
                return prices[candidate]
        return None

    def compute(
        self,
        upstream: str,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        cache_read: int = 0,
        cache_write: int = 0,
        at: datetime | None = None,
    ) -> float | None:
        """计算单次请求成本（USD）。无价格返回 None。"""
        price = self.get(upstream, model)
        if price is None:
            return None

        cost = (
            input_tokens / 1_000_000 * price.input
            + output_tokens / 1_000_000 * price.output
            + cache_read / 1_000_000 * price.cache_read
            + cache_write / 1_000_000 * price.cache_write
        )

        factor = _multiplier_factor(price.multipliers, at)
        return round(cost * factor, 8)

    def breakdown(
        self,
        upstream: str,
        model: str | None,
        input_tokens: int,
        output_tokens: int = 0,
        cache_read: int = 0,
        cache_write: int = 0,
        at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """单次请求的费用分项拆解（单价、时段倍率、各项费用与缓存节省额）。

        与 compute() 同一套口径；无价格返回 None。
        cache_saved_usd = 命中的 token 若按「未命中输入单价」计费要多花的钱，
        即 prompt cache 在这一次调用里实打实省下的金额。
        """
        price = self.get(upstream, model)
        if price is None:
            return None

        factor = _multiplier_factor(price.multipliers, at)
        k = factor / 1_000_000
        input_cost = input_tokens * price.input * k
        output_cost = output_tokens * price.output * k
        cache_read_cost = cache_read * price.cache_read * k
        cache_write_cost = cache_write * price.cache_write * k
        total = input_cost + output_cost + cache_read_cost + cache_write_cost
        cache_saved = cache_read * max(0.0, price.input - price.cache_read) * k

        return {
            "units": {                      # 单价：USD / 1M tokens
                "input": price.input,
                "output": price.output,
                "cache_read": price.cache_read,
                "cache_write": price.cache_write,
            },
            "multiplier": factor,
            "input_cost": round(input_cost, 8),
            "output_cost": round(output_cost, 8),
            "cache_read_cost": round(cache_read_cost, 8),
            "cache_write_cost": round(cache_write_cost, 8),
            "total_cost": round(total, 8),
            "cache_saved_usd": round(cache_saved, 8),
        }


def _multiplier_factor(multipliers: list[dict], at: datetime | None) -> float:
    """计算当前时段倍率。"""
    if not multipliers:
        return 1.0
    now = at or datetime.now(timezone.utc)
    day = now.strftime("%a").lower()[:3]
    hhmm = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")

    for rule in multipliers:
        when = rule.get("when") or {}
        if not _match_when(when, day, hhmm, today):
            continue
        try:
            return float(rule.get("factor") or 1.0)
        except (TypeError, ValueError):
            continue
    return 1.0


def _match_when(when: dict, day: str, hhmm: str, today: str = "") -> bool:
    """匹配 when 条件。

    支持：
      utc_days   —— 星期过滤 {"in": ["mon","tue"]}
      utc_hours  —— 小时区间 {"gte": "01:00", "lt": "04:00"}
      after      —— 该日期及之后生效 "2026-12-01"
      before     —— 该日期之前生效 "2027-01-01"
      all / any  —— 逻辑组合
    """
    days = when.get("utc_days")
    if isinstance(days, dict):
        allowed = days.get("in")
        if isinstance(allowed, list) and day not in [str(d).lower()[:3] for d in allowed]:
            return False

    hours = when.get("utc_hours")
    if isinstance(hours, dict):
        gte = hours.get("gte")
        lt = hours.get("lt")
        if gte and hhmm < str(gte):
            return False
        if lt and hhmm >= str(lt):
            return False

    # 日期区间（用于官方调价）
    if today:
        after = when.get("after")
        if after and today < str(after)[:10]:
            return False
        before = when.get("before")
        if before and today >= str(before)[:10]:
            return False

    all_conds = when.get("all")
    if isinstance(all_conds, list) and all_conds:
        if not all(_match_when(c, day, hhmm, today) for c in all_conds if isinstance(c, dict)):
            return False

    any_conds = when.get("any")
    if isinstance(any_conds, list) and any_conds:
        if not any(_match_when(c, day, hhmm, today) for c in any_conds if isinstance(c, dict)):
            return False

    return True


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------

def load_prices(pricing_path: Path, auto_dirs: dict[str, Path] | None = None) -> PriceTable:
    """加载价格表。

    pricing_path: pricing.yaml（手工配置）
    auto_dirs:    {upstream_name: json_path}，自动拉取的 JSON（如 Zen）
    """
    table: dict[str, dict[str, ModelPrice]] = {}

    # 1. 手工配置
    if pricing_path.exists():
        try:
            import yaml
            raw = yaml.safe_load(pricing_path.read_text(encoding="utf-8")) or {}
            for up_name, models in (raw.get("upstreams") or {}).items():
                table.setdefault(up_name, {})
                for model, spec in (models or {}).items():
                    if not isinstance(spec, dict):
                        continue
                    table[up_name][model] = ModelPrice(
                        input=float(spec.get("input") or 0),
                        output=float(spec.get("output") or 0),
                        cache_read=float(spec.get("cache_read") or 0),
                        cache_write=float(spec.get("cache_write") or 0),
                        multipliers=list(spec.get("multipliers") or []),
                    )
            log.info("loaded pricing.yaml: %d upstreams", len(table))
        except Exception as exc:
            log.warning("failed to load %s: %s", pricing_path, exc)

    # 2. 自动拉取（Zen）
    for up_name, json_path in (auto_dirs or {}).items():
        if not json_path.exists():
            continue
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
            table.setdefault(up_name, {})
            for model, spec in raw.items():
                if not isinstance(spec, dict):
                    continue
                table[up_name][model] = ModelPrice(
                    input=float(spec.get("input") or 0),
                    output=float(spec.get("output") or 0),
                    cache_read=float(spec.get("cache_read") or 0),
                    cache_write=float(spec.get("cache_write") or 0),
                    multipliers=list(spec.get("multipliers") or []),
                )
            log.info("loaded auto prices for %s: %d models", up_name, len(raw))
        except Exception as exc:
            log.warning("failed to load %s: %s", json_path, exc)

    return PriceTable(table)
