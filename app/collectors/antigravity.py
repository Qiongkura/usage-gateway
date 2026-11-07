"""Antigravity Tools 采集器。

数据源：
  - token_stats.db  -> token_usage 表（真实 token 数，未缩放）
  - accounts/*.json -> quota.quota_groups（额度百分比、重置时间）

注意：Antigravity Tools 运行时会对 SQLite 加锁，直接只读打开可能失败。
这里先复制到临时目录再读，避免干扰它的写入。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

from ..models import QuotaSnapshot, UsageRecord
from .base import Collector


def default_data_dir() -> Path:
    return Path(os.path.expanduser("~/.antigravity_tools"))


def _parse_iso(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def _snapshot_copy(src: Path) -> Path | None:
    """把 SQLite 主库 + wal/shm 复制到临时目录，返回副本路径。"""
    if not src.exists():
        return None
    tmpdir = Path(tempfile.mkdtemp(prefix="agdb-"))
    dst = tmpdir / src.name
    try:
        shutil.copy2(src, dst)
        for suffix in ("-wal", "-shm"):
            side = src.with_name(src.name + suffix)
            if side.exists():
                shutil.copy2(side, dst.with_name(dst.name + suffix))
        return dst
    except OSError:
        return None


class AntigravityCollector(Collector):
    name = "antigravity"

    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(os.path.expanduser(str(data_dir))) if data_dir else default_data_dir()
        self.stats_db = self.data_dir / "token_stats.db"
        self.accounts_dir = self.data_dir / "accounts"

    # ---------- 用量 ----------

    def collect_usage(self, since_ts: int) -> Iterable[UsageRecord]:
        copy = _snapshot_copy(self.stats_db)
        if copy is None:
            return []
        try:
            conn = sqlite3.connect(copy)
            conn.row_factory = sqlite3.Row
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(token_usage)")}
            has_reasoning = "reasoning_tokens" in cols
            has_requested = "requested_model" in cols
            has_ttft = "ttft_ms" in cols

            select = [
                "timestamp", "account_email", "model",
                "input_tokens", "output_tokens", "cached_tokens",
                "reasoning_tokens" if has_reasoning else "0 AS reasoning_tokens",
                "requested_model" if has_requested else "NULL AS requested_model",
                "ttft_ms" if has_ttft else "NULL AS ttft_ms",
            ]
            rows = conn.execute(
                f"SELECT {', '.join(select)} FROM token_usage "
                "WHERE timestamp > ? ORDER BY timestamp ASC",
                (since_ts,),
            ).fetchall()
            conn.close()
        except sqlite3.Error:
            return []
        finally:
            shutil.rmtree(copy.parent, ignore_errors=True)

        for r in rows:
            yield UsageRecord(
                ts=int(r["timestamp"]),
                source=self.name,
                account=r["account_email"],
                model=r["model"],
                requested_model=r["requested_model"],
                input_tokens=int(r["input_tokens"] or 0),
                output_tokens=int(r["output_tokens"] or 0),
                cache_read=int(r["cached_tokens"] or 0),
                reasoning=int(r["reasoning_tokens"] or 0),
                ttft_ms=float(r["ttft_ms"]) if r["ttft_ms"] is not None else None,
            )

    # ---------- 额度 ----------

    def collect_quota(self) -> Iterable[QuotaSnapshot]:
        if not self.accounts_dir.exists():
            return []
        now = int(time.time())
        for account_file in self.accounts_dir.glob("*.json"):
            try:
                data = json.loads(account_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            email = data.get("email") or account_file.stem
            quota = data.get("quota") or {}

            for group in quota.get("quota_groups") or []:
                group_name = (group.get("display_name") or "").strip()
                for bucket in group.get("buckets") or []:
                    frac = bucket.get("remaining_fraction")
                    if frac is None:
                        continue
                    window = bucket.get("window") or "unknown"
                    label = f"{group_name}:{window}" if group_name else window
                    yield QuotaSnapshot(
                        ts=now,
                        source=self.name,
                        account=email,
                        window=label,
                        remaining_pct=round(float(frac) * 100, 2),
                        resets_at=_parse_iso(bucket.get("reset_time")),
                    )

            # 老版本只有 models 列表（每个模型一个百分比）
            for m in quota.get("models") or []:
                pct = m.get("percentage")
                if pct is None:
                    continue
                yield QuotaSnapshot(
                    ts=now,
                    source=self.name,
                    account=email,
                    window=f"model:{m.get('name') or 'unknown'}",
                    remaining_pct=float(pct),
                    resets_at=_parse_iso(m.get("reset_time")),
                )
