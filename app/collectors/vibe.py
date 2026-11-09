"""Mistral Vibe 本地日志采集器。

数据源：$VIBE_HOME/logs/session/<sid>/meta.json

两个关键口径：
  1. session_prompt_tokens 已包含 cached，输入要减掉避免重复计算；
  2. 会话级统计是「累计值」，活跃会话每分钟都会变大 —— 直接整份入账会重复统计。
     这里按「上次快照的差值」入账；调度器将快照与用量一同存入数据库。
     首次迁移时读取原有的 data_dir/vibe_state.json。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Iterable

from ..models import UsageRecord
from .base import Collector

log = logging.getLogger(__name__)


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
        previous = self._last
        records, state = self._collect(since_ts, previous, inclusive=False)
        self._last = state
        if state != previous:
            self._save_state()
        return records

    def prepare_usage(self, since_ts: int, cursor: str | None) -> tuple[list[UsageRecord], str]:
        if cursor is None:
            state = self._load_state()
        else:
            state = json.loads(cursor)
            if not isinstance(state, dict):
                raise ValueError("invalid Vibe collector state")
        records, state = self._collect(since_ts, state, inclusive=True)
        return records, json.dumps(state, ensure_ascii=False)

    def _collect(self, since_ts: int, previous: dict[str, dict], *, inclusive: bool
                 ) -> tuple[list[UsageRecord], dict[str, dict]]:
        state = previous.copy()
        session_root = self.home / "logs" / "session"
        try:
            scan = os.scandir(session_root)
        except FileNotFoundError:
            return [], state
        with scan as entries:
            session_dirs = [Path(entry.path) for entry in entries if entry.is_dir()]

        records: list[UsageRecord] = []
        for session_dir in session_dirs:
            meta_path = session_dir / "meta.json"
            sid = meta_path.parent.name
            try:
                mtime = int(meta_path.stat().st_mtime)
            except OSError:
                if inclusive and sid not in state:
                    state[sid] = {"pending": True}
                continue
            if (not inclusive and mtime <= since_ts) or (
                inclusive and sid not in state and mtime < since_ts
            ):
                continue
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                if inclusive and sid not in state:
                    state[sid] = {"pending": True}
                continue

            if not isinstance(data, dict):
                if inclusive and sid not in state:
                    state[sid] = {"pending": True}
                continue
            keys = ("session_prompt_tokens", "session_cached_tokens", "session_completion_tokens")
            stats = next((candidate for candidate in
                          (data.get("metadata"), data.get("session_stats"), data.get("stats"), data)
                          if isinstance(candidate, dict) and any(key in candidate for key in keys)), None)
            prev = state.get(sid)
            pending = isinstance(prev, dict) and prev.get("pending") is True
            if pending:
                prev = None
            if stats is None:
                if inclusive and sid not in state:
                    state[sid] = {"pending": True}
                continue
            try:
                def counter(key: str, previous_key: str) -> int:
                    value = stats.get(key)
                    return int(value if value is not None else (prev.get(previous_key, 0) if prev else 0))

                prompt = counter("session_prompt_tokens", "t")
                cached = counter("session_cached_tokens", "c")
                completion = counter("session_completion_tokens", "o")
            except (TypeError, ValueError):
                if inclusive and sid not in state:
                    state[sid] = {"pending": True}
                continue
            if min(prompt, cached, completion) < 0:
                if inclusive and sid not in state:
                    state[sid] = {"pending": True}
                continue
            if prompt == 0 and cached == 0 and completion == 0 and not prev:
                if inclusive:
                    state[sid] = {"t": 0, "c": 0, "o": 0, "mtime": mtime, "seq": 0}
                continue
            base_in = int(prev["t"]) if prev else 0
            base_cache = int(prev["c"]) if prev else 0
            base_out = int(prev["o"]) if prev else 0
            # A decreasing counter starts a new observed cycle for that counter.
            d_prompt = prompt - base_in if prompt >= base_in else prompt
            d_cached = cached - base_cache if cached >= base_cache else cached
            d_out = completion - base_out if completion >= base_out else completion
            if d_cached > d_prompt:
                log.warning(
                    "Vibe cached-token increment exceeds prompt increment; "
                    "recorded token totals may be overstated"
                )
            changed = (prompt, cached, completion) != (base_in, base_cache, base_out)
            if not changed:
                continue

            sequence = int(prev.get("seq", 0)) + 1 if prev else 1
            state[sid] = {"t": prompt, "c": cached, "o": completion, "mtime": mtime, "seq": sequence}
            if d_prompt == 0 and d_cached == 0 and d_out == 0:
                continue
            identity = json.dumps([sid, sequence, mtime, prompt, cached, completion], ensure_ascii=False)
            records.append(UsageRecord(
                ts=mtime,
                source=self.name,
                account=sid,
                model=stats.get("model") or data.get("model"),
                input_tokens=max(0, d_prompt - d_cached),
                cache_read=d_cached,
                output_tokens=d_out,
                raw={"request_id": "vibe-snapshot-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()}
                if inclusive else None,
            ))

        if not inclusive and len(state) > self.MAX_TRACKED_SESSIONS:
            for sid in sorted(
                state, key=lambda s: state[s].get("mtime", 0)
            )[: len(state) - self.MAX_TRACKED_SESSIONS]:
                state.pop(sid, None)
        return records, state
