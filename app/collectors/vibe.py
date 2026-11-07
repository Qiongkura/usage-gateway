"""Mistral Vibe 本地日志采集器。

数据源：$VIBE_HOME/logs/session/<sid>/meta.json

两个关键口径：
  1. session_prompt_tokens 已包含 cached，输入要减掉避免重复计算；
  2. 会话级统计是「累计值」，活跃会话每分钟都会变大 —— 直接整份入账会重复统计。
     这里按「上次快照的差值」入账，状态持久化到 data_dir/vibe_state.json，
     网关重启后也不会把同一会话的累计值重复算一遍。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

from ..models import UsageRecord
from .base import Collector


def default_home() -> Path:
    return Path(os.path.expanduser(os.environ.get("VIBE_HOME", "~/.vibe")))


class VibeCollector(Collector):
    name = "vibe"

    # 状态里最多保留多少个会话（活跃会话远小于这个数）
    MAX_TRACKED_SESSIONS = 200

    def __init__(self, home: str | Path | None = None, state_file: str | Path | None = None):
        self.home = Path(os.path.expanduser(str(home))) if home else default_home()
        self.state_file = Path(state_file) if state_file else None
        self._last: dict[str, dict] = self._load_state()

    # ---------- 状态持久化 ----------

    def _load_state(self) -> dict[str, dict]:
        if not self.state_file or not self.state_file.exists():
            return {}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        if not self.state_file:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps(self._last, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    # ---------- 采集 ----------

    def collect_usage(self, since_ts: int) -> Iterable[UsageRecord]:
        session_root = self.home / "logs" / "session"
        if not session_root.exists():
            return []

        changed = False
        records: list[UsageRecord] = []
        for meta_path in session_root.glob("*/meta.json"):
            try:
                mtime = int(meta_path.stat().st_mtime)
                if mtime <= since_ts:
                    continue
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            stats = data.get("metadata") or data.get("session_stats") or data
            prompt = int(stats.get("session_prompt_tokens", 0) or 0)
            cached = int(stats.get("session_cached_tokens", 0) or 0)
            completion = int(stats.get("session_completion_tokens", 0) or 0)
            if prompt == 0 and completion == 0:
                continue

            sid = meta_path.parent.name
            prev = self._last.get(sid)
            base_in = int(prev["t"]) if prev else 0
            base_cache = int(prev["c"]) if prev else 0
            base_out = int(prev["o"]) if prev else 0
            # 累计值理论上单调递增；出现回退（新周期重置）时按 0 记，避免负数
            d_prompt = max(0, prompt - base_in)
            d_cached = max(0, cached - base_cache)
            d_out = max(0, completion - base_out)
            if d_prompt == 0 and d_cached == 0 and d_out == 0:
                continue

            self._last[sid] = {"t": prompt, "c": cached, "o": completion, "mtime": mtime}
            changed = True
            records.append(UsageRecord(
                ts=mtime,
                source=self.name,
                account=sid,
                model=stats.get("model") or data.get("model"),
                input_tokens=max(0, d_prompt - d_cached),
                cache_read=d_cached,
                output_tokens=d_out,
            ))

        if changed:
            if len(self._last) > self.MAX_TRACKED_SESSIONS:
                for sid in sorted(
                    self._last, key=lambda s: self._last[s].get("mtime", 0)
                )[: len(self._last) - self.MAX_TRACKED_SESSIONS]:
                    self._last.pop(sid, None)
            self._save_state()
        yield from records
