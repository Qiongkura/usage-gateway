"""额度告警。

设计要点：

1. **只判断「剩余百分比」**。百分比类窗口（订阅额度）和金额类窗口（余额）
   都先折算成百分比，判断逻辑只有一套。
2. **状态机 + 冷却**。每个 (source, account, window) 维护一条记录：
   当前等级、上次提醒时间、上次提醒时的等级。只有「等级变差」或
   「冷却到期后仍然超标」才提醒，避免每分钟刷屏。
3. **恢复也提醒一次**。额度重置（例如周窗口回到 100%）时告诉你一声，
   否则你只会记得它快没了。
4. **数据过期不判断**。采集器挂了（例如 Antigravity Tools 没开）时，
   最后一次快照可能停在 10%，那不该反复告警。
5. **通知失败不影响主流程**。所有渠道异常都吞掉并记日志。
"""
from __future__ import annotations

import fnmatch
import json
import logging
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable

from .config import QuotaAlertConfig

log = logging.getLogger(__name__)

LEVEL_OK = 0
LEVEL_WARN = 1
LEVEL_CRITICAL = 2

LEVEL_NAMES = {LEVEL_OK: "ok", LEVEL_WARN: "warn", LEVEL_CRITICAL: "critical"}

SOURCE_LABELS = {
    "antigravity": "Antigravity",
    "mistral": "Mistral",
    "opencode": "OpenCode Zen/Go",
    "relay-axon": "axon 中转站",
}


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source)


@dataclass
class QuotaItem:
    """一条额度快照（已折算成百分比）。"""

    source: str
    account: str
    window: str
    remaining_pct: float
    remaining_amt: float | None = None
    currency: str | None = None
    resets_at: int | None = None
    ts: int = 0
    level: int = LEVEL_OK
    stale: bool = False
    # 燃烧率：按近期消耗速度预测
    burn_per_hour: float | None = None  # 每小时掉的百分点
    burn_per_day: float | None = None   # 每天掉的百分点（= per_hour × 24）
    hours_left: float | None = None     # 按当前速率预计耗尽小时数
    days_left: float | None = None      # 同上，换算成天（不考虑重置）
    hours_to_reset: float | None = None     # 距离窗口重置还有多少小时
    projected_pct: float | None = None      # 按当前速率到重置时预计还剩多少
    exhaust_before_reset: bool = False      # 会不会在重置前耗尽

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.source, self.account, self.window)

    @property
    def label(self) -> str:
        return f"{source_label(self.source)} · {self.account} · {self.window}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "account": self.account,
            "window": self.window,
            "remaining_pct": round(self.remaining_pct, 2),
            "remaining_amt": self.remaining_amt,
            "currency": self.currency,
            "resets_at": self.resets_at,
            "ts": self.ts,
            "level": LEVEL_NAMES.get(self.level, "ok"),
            "stale": self.stale,
            "burn_per_hour": self.burn_per_hour,
            "burn_per_day": self.burn_per_day,
            "hours_left": self.hours_left,
            "days_left": self.days_left,
            "hours_to_reset": self.hours_to_reset,
            "projected_pct": self.projected_pct,
            "exhaust_before_reset": self.exhaust_before_reset,
        }


@dataclass
class AlertState:
    """一个窗口的告警状态。"""

    level: int = LEVEL_OK
    last_notified_level: int = LEVEL_OK
    last_notified_at: float = 0.0
    last_value: float | None = None
    # 最近一次提醒的内容，用于 /api/alerts 展示
    last_message: str = ""


@dataclass
class AlertEvent:
    """一次需要通知的事件。"""

    kind: str            # "warn" | "critical" | "recovered"
    item: QuotaItem
    previous_level: int
    message: str


def classify(pct: float, cfg: QuotaAlertConfig) -> int:
    if pct <= cfg.critical_pct:
        return LEVEL_CRITICAL
    if pct <= cfg.warn_pct:
        return LEVEL_WARN
    return LEVEL_OK


def is_ignored(source: str, account: str, window: str, cfg: QuotaAlertConfig) -> bool:
    """ignore_windows 支持通配符，可匹配 window、account 或 "source:window"。"""
    candidates = (window, account, f"{source}:{window}")
    for pattern in cfg.ignore_windows:
        for value in candidates:
            if fnmatch.fnmatchcase(value, pattern):
                return True
    return False


def _fmt_reset(resets_at: int | None) -> str:
    if not resets_at:
        return ""
    delta = resets_at - int(time.time())
    if delta <= 0:
        return "即将重置"
    if delta >= 86400:
        return f"{delta / 86400:.1f} 天后重置"
    if delta >= 3600:
        return f"{delta / 3600:.1f} 小时后重置"
    return f"{max(1, delta // 60)} 分钟后重置"


def _fmt_amount(item: QuotaItem) -> str:
    if item.remaining_amt is None:
        return ""
    cur = item.currency or "USD"
    return f"（剩 {cur} {item.remaining_amt:.2f}）"


def build_message(kind: str, item: QuotaItem) -> str:
    pct = f"{item.remaining_pct:.1f}%"
    if kind == "recovered":
        head = f"✅ 额度已恢复：{item.label} 剩 {pct}"
    elif kind == "critical":
        head = f"🔴 额度告急：{item.label} 只剩 {pct}"
    else:
        head = f"🟠 额度偏低：{item.label} 剩 {pct}"
    tail = " ".join(x for x in (_fmt_amount(item), _fmt_reset(item.resets_at)) if x)
    if item.exhaust_before_reset and item.hours_left is not None:
        # 只报「重置前真的会用完」的预测；百分比/天数在没有重置时间时才有意义
        if item.hours_left >= 48:
            tail += f" · 按当前速率约 {item.hours_left / 24:.1f} 天耗尽"
        else:
            tail += f" · 按当前速率约 {item.hours_left:.0f} 小时耗尽"
    elif item.hours_to_reset is None and item.days_left is not None and item.days_left <= 30:
        tail += f" · 按当前速率约 {item.days_left:.1f} 天耗尽"
    return f"{head} {tail}".strip()


class QuotaAlerter:
    """把额度快照变成告警事件。无状态判断 + 有状态冷却。"""

    def __init__(self, cfg: QuotaAlertConfig):
        self.cfg = cfg
        self.states: dict[tuple[str, str, str], AlertState] = {}
        self.history: list[dict[str, Any]] = []
        self.notifier = Notifier(cfg.notify)

    # ---------- 核心 ----------

    def evaluate(self, items: Iterable[QuotaItem], now: float | None = None) -> list[AlertEvent]:
        """更新状态并返回本轮需要通知的事件。"""
        now = now if now is not None else time.time()
        events: list[AlertEvent] = []

        for item in items:
            if item.stale or is_ignored(item.source, item.account, item.window, self.cfg):
                continue

            level = classify(item.remaining_pct, self.cfg)
            # 燃烧率预警：还没到告警阈值，但按当前速率会在重置前耗尽（48h 内）
            # → 提前预警。有重置时间时不再用「天数」判断，避免 5h 窗口误报。
            if level == LEVEL_OK and item.remaining_pct <= 90:
                if (
                    item.exhaust_before_reset
                    and item.hours_left is not None
                    and item.hours_left <= 48
                ):
                    level = LEVEL_WARN
                elif (
                    item.hours_to_reset is None
                    and item.days_left is not None
                    and item.days_left <= 3
                ):
                    level = LEVEL_WARN
            item.level = level
            state = self.states.get(item.key)
            if state is None:
                state = AlertState(level=level, last_value=item.remaining_pct)
                self.states[item.key] = state
                # 首次见到就超标也要提醒
                if level > LEVEL_OK:
                    events.append(self._fire(item, state, level, now))
                continue

            previous = state.level
            state.level = level
            state.last_value = item.remaining_pct

            if level > previous:
                # 变差：立刻提醒
                events.append(self._fire(item, state, level, now))
            elif level == LEVEL_OK and previous > LEVEL_OK:
                # 恢复：无条件提醒一次。额度重置是好消息，不该被冷却挡掉。
                event = AlertEvent(
                    kind="recovered",
                    item=item,
                    previous_level=previous,
                    message=build_message("recovered", item),
                )
                state.last_notified_level = LEVEL_OK
                state.last_notified_at = now
                state.last_message = event.message
                events.append(event)
            elif level > LEVEL_OK and now - state.last_notified_at >= self.cfg.cooldown_sec:
                # 一直超标：冷却到期后重提一次
                events.append(self._fire(item, state, level, now))

        for event in events:
            self._record(event, now)
        return events

    def _fire(self, item: QuotaItem, state: AlertState, level: int, now: float) -> AlertEvent:
        kind = "critical" if level == LEVEL_CRITICAL else "warn"
        event = AlertEvent(
            kind=kind,
            item=item,
            previous_level=state.last_notified_level,
            message=build_message(kind, item),
        )
        state.last_notified_level = level
        state.last_notified_at = now
        state.last_message = event.message
        return event

    def _record(self, event: AlertEvent, now: float) -> None:
        self.history.append({
            "ts": int(now),
            "kind": event.kind,
            "source": event.item.source,
            "account": event.item.account,
            "window": event.item.window,
            "remaining_pct": round(event.item.remaining_pct, 2),
            "message": event.message,
        })
        # 只留最近 200 条
        if len(self.history) > 200:
            del self.history[:-200]
        log.warning("[quota-alert] %s", event.message)
        try:
            self.notifier.send(event.message)
        except Exception:
            log.exception("[quota-alert] notify failed")

    # ---------- 对外视图 ----------

    def snapshot(self, items: Iterable[QuotaItem]) -> dict[str, Any]:
        """给 /api/alerts 用的完整视图（不触发通知）。"""
        rows = []
        for item in items:
            if is_ignored(item.source, item.account, item.window, self.cfg):
                continue
            state = self.states.get(item.key)
            item.level = classify(item.remaining_pct, self.cfg) if not item.stale else LEVEL_OK
            rows.append({
                **item.to_dict(),
                "last_notified_at": int(state.last_notified_at) if state else 0,
                "last_message": state.last_message if state else "",
            })
        rows.sort(key=lambda r: (r["level"] != "critical", r["level"] != "warn", r["remaining_pct"]))
        return {
            "config": {
                "enabled": self.cfg.enabled,
                "warn_pct": self.cfg.warn_pct,
                "critical_pct": self.cfg.critical_pct,
                "cooldown_sec": self.cfg.cooldown_sec,
                "ignore_windows": self.cfg.ignore_windows,
                "channels": self.notifier.active_channels(),
            },
            "items": rows,
            "counts": {
                "critical": sum(1 for r in rows if r["level"] == "critical"),
                "warn": sum(1 for r in rows if r["level"] == "warn"),
                "ok": sum(1 for r in rows if r["level"] == "ok"),
            },
            "history": list(reversed(self.history[-50:])),
        }


class Notifier:
    """通知渠道。目前支持桌面通知、webhook、日志。

    配置示例（config.yaml）：

        alerts:
          notify:
            desktop: true
            webhook: "https://example.com/hook"
    """

    def __init__(self, raw: dict[str, Any]):
        self.raw = raw or {}
        self.desktop = bool(self.raw.get("desktop"))
        self.webhook = str(self.raw.get("webhook") or "").strip()
        self._ps = None
        if self.desktop:
            try:
                import subprocess
                self._ps = subprocess
            except Exception:
                self.desktop = False

    def active_channels(self) -> list[str]:
        channels = ["log"]
        if self.desktop:
            channels.append("desktop")
        if self.webhook:
            channels.append("webhook")
        return channels

    def send(self, message: str) -> None:
        if self.desktop:
            self._send_desktop(message)
        if self.webhook:
            self._send_webhook(message)

    def _send_desktop(self, message: str) -> None:
        """Windows 用 PowerShell 气泡通知；其他平台静默跳过。"""
        if not self._ps:
            return
        try:
            import sys
            if sys.platform != "win32":
                return
            # 单引号转义，避免消息里的引号破坏脚本
            safe = message.replace("'", "''")
            script = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "Add-Type -AssemblyName System.Drawing;"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Warning;"
                "$n.Visible=$true;"
                f"$n.ShowBalloonTip(10000,'Usage Gateway','{safe}',"
                "[System.Windows.Forms.ToolTipIcon]::Warning);"
                "Start-Sleep -Seconds 6;$n.Dispose()"
            )
            self._ps.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                stdout=self._ps.DEVNULL,
                stderr=self._ps.DEVNULL,
                creationflags=getattr(self._ps, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            log.exception("[quota-alert] desktop notify failed")

    def _send_webhook(self, message: str) -> None:
        try:
            payload = json.dumps({
                "text": message,
                "content": message,
                "msgtype": "text",
                "msg": message,
            }).encode("utf-8")
            req = urllib.request.Request(
                self.webhook,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "usage-gateway/0.1.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read(256)
        except Exception:
            log.exception("[quota-alert] webhook notify failed")
