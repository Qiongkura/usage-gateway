"""OpenCode 本地日志采集器。

数据源：~/.local/share/opencode/opencode.db
表结构随版本变化，这里做兼容探测。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterable

from ..models import UsageRecord
from .base import Collector


def default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "opencode"
    return Path(os.path.expanduser("~/.local/share/opencode"))


class OpenCodeCollector(Collector):
    name = "opencode"

    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(os.path.expanduser(str(data_dir))) if data_dir else default_data_dir()
        self.db_path = self.data_dir / "opencode.db"

    def collect_usage(self, since_ts: int) -> Iterable[UsageRecord]:
        if not self.db_path.exists():
            return []

        tmpdir = Path(tempfile.mkdtemp(prefix="ocdb-"))
        copy = tmpdir / "opencode.db"
        try:
            shutil.copy2(self.db_path, copy)
            for suffix in ("-wal", "-shm"):
                side = self.db_path.with_name(self.db_path.name + suffix)
                if side.exists():
                    shutil.copy2(side, copy.with_name(copy.name + suffix))
            conn = sqlite3.connect(copy)
            conn.row_factory = sqlite3.Row
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except (OSError, sqlite3.Error):
            shutil.rmtree(tmpdir, ignore_errors=True)
            return []

        records: list[UsageRecord] = []
        try:
            # 新版：session_message / message 表里带 usage JSON
            for table in ("session_message", "message"):
                if table not in tables:
                    continue
                cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                time_col = next(
                    (c for c in ("time_created", "created_at", "created", "time") if c in cols),
                    None,
                )
                if not time_col:
                    continue
                try:
                    rows = conn.execute(
                        f"SELECT * FROM {table} WHERE {time_col} > ? ORDER BY {time_col} ASC",
                        (since_ts * 1000,),
                    ).fetchall()
                except sqlite3.Error:
                    continue

                for row in rows:
                    rec = self._row_to_record(dict(row), time_col)
                    if rec:
                        records.append(rec)
                if records:
                    break
        finally:
            conn.close()
            shutil.rmtree(tmpdir, ignore_errors=True)

        yield from records

    @staticmethod
    def _row_to_record(row: dict, time_col: str) -> UsageRecord | None:
        raw = row.get("data") or row.get("message") or row.get("content")
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return None
        elif isinstance(raw, dict):
            data = raw
        else:
            return None

        usage = data.get("usage") or data.get("tokens") or {}
        if not usage:
            return None
        tokens = usage.get("tokens") if isinstance(usage.get("tokens"), dict) else usage

        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        ts_raw = row.get(time_col) or 0
        ts = int(ts_raw / 1000) if ts_raw > 10_000_000_000 else int(ts_raw)

        return UsageRecord(
            ts=ts,
            source="opencode",
            model=data.get("modelID") or data.get("model") or usage.get("model"),
            input_tokens=int(tokens.get("input", 0) or 0),
            output_tokens=int(tokens.get("output", 0) or 0),
            cache_read=int(cache.get("read", 0) or 0),
            cache_write=int(cache.get("write", 0) or 0),
            reasoning=int(tokens.get("reasoning", 0) or 0),
        )
