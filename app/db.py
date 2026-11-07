"""SQLite 存储层。"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

from .models import QuotaSnapshot, UsageRecord, dumps

SCHEMA = """
PRAGMA journal_mode=WAL;
-- WAL 模式下 NORMAL 保证进程崩溃不坏库、不丢 WAL 页，
-- 同时避免每次 commit 调 Windows FlushFileBuffers 阻塞流式事件循环。
-- 优雅退出时仍由 close()/checkpoint() 执行 FULL checkpoint 落盘。
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;
-- WAL 每 1000 页自动 checkpoint
PRAGMA wal_autocheckpoint=1000;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS usage_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key      TEXT UNIQUE NOT NULL,
    ts              INTEGER NOT NULL,
    source          TEXT NOT NULL,
    account         TEXT,
    model           TEXT,
    requested_model TEXT,
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    cache_read      INTEGER DEFAULT 0,
    cache_write     INTEGER DEFAULT 0,
    reasoning       INTEGER DEFAULT 0,
    cost_usd        REAL,
    ttft_ms         REAL,
    latency_ms      REAL,
    status          INTEGER DEFAULT 200,
    client_key      TEXT,
    raw_json        TEXT,
    ingested_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_ts     ON usage_records(ts);
CREATE INDEX IF NOT EXISTS idx_usage_source ON usage_records(source, ts);
CREATE INDEX IF NOT EXISTS idx_usage_model  ON usage_records(model, ts);
CREATE INDEX IF NOT EXISTS idx_usage_key    ON usage_records(client_key, ts);
-- 调用明细日志（/api/logs）：按时间倒序分页
CREATE INDEX IF NOT EXISTS idx_usage_ts_id  ON usage_records(ts DESC, id DESC);

CREATE TABLE IF NOT EXISTS quota_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    source        TEXT NOT NULL,
    account       TEXT,
    window        TEXT,
    remaining_pct REAL,
    remaining_amt REAL,
    currency      TEXT,
    resets_at     INTEGER,
    raw_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_quota_ts ON quota_snapshots(ts DESC);
-- /api/quota 与告警判断都要按 (source, account, window) 取最新一条；
-- 没有这个复合索引时每次都全表 GROUP BY，快照攒到几万行就开始卡。
CREATE INDEX IF NOT EXISTS idx_quota_lookup
    ON quota_snapshots(source, account, window, ts DESC);

CREATE TABLE IF NOT EXISTS collector_state (
    collector   TEXT PRIMARY KEY,
    last_ts     INTEGER DEFAULT 0,
    last_cursor TEXT,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS request_traces (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id        TEXT UNIQUE NOT NULL,
    ts              INTEGER NOT NULL,
    source          TEXT,
    model           TEXT,
    requested_model TEXT,
    client_key      TEXT,
    status          INTEGER,
    total_ms        REAL,
    spans_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_trace_ts     ON request_traces(ts DESC);
CREATE INDEX IF NOT EXISTS idx_trace_status ON request_traces(status, ts DESC);
"""


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._writes = 0
        # APScheduler 后台线程写、FastAPI 工作线程读写：
        # 写操作统一走 self.conn + self.lock；读操作用线程本地 WAL 连接（read_conn），
        # 既彻底消除多线程共用同一 sqlite3.Connection 的事务竞争，又让读写互不阻塞。
        self.lock = threading.RLock()
        self._local = threading.local()
        self._read_conns: list[sqlite3.Connection] = []

    def read_conn(self) -> sqlite3.Connection:
        """获取当前线程专属的只读查询连接（WAL 下与写连接并发无锁冲突）。"""
        if str(self.path) == ":memory:":
            return self.conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA temp_store=MEMORY")
            self._local.conn = conn
            with self.lock:
                self._read_conns.append(conn)
        return conn

    def _maybe_checkpoint(self) -> None:
        """每 50 次写入做一次 checkpoint，避免 WAL 无限增长。"""
        with self.lock:
            self._writes += 1
            if self._writes % 50 == 0:
                try:
                    self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except Exception:
                    pass

    def checkpoint(self) -> None:
        with self.lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(FULL)")
            except Exception:
                pass

    # ---------- 用量 ----------

    def insert_usage(self, records: Iterable[UsageRecord]) -> int:
        now = int(time.time())
        inserted = 0
        with self.lock:
            for r in records:
                # 本地采集器（antigravity / opencode / vibe）拿到的是上游自己记的账，
                # 而同一笔请求如果也经过了网关，网关已经记过一条（client_key 非空）。
                # 两边 dedupe_key 格式不同，只能靠「同源 + 时间贴近 + token 完全相同」识别。
                if r.client_key is None and self._seen_from_gateway(r):
                    continue
                cur = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO usage_records
                    (dedupe_key, ts, source, account, model, requested_model,
                     input_tokens, output_tokens, cache_read, cache_write, reasoning,
                     cost_usd, ttft_ms, latency_ms, status, client_key, raw_json, ingested_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        r.dedupe_key(), r.ts, r.source, r.account, r.model, r.requested_model,
                        r.input_tokens, r.output_tokens, r.cache_read, r.cache_write, r.reasoning,
                        r.cost_usd, r.ttft_ms, r.latency_ms, r.status, r.client_key,
                        dumps(r.raw), now,
                    ),
                )
                inserted += cur.rowcount
            self.conn.commit()
        self._maybe_checkpoint()
        return inserted

    def _seen_from_gateway(self, r: UsageRecord, window_sec: int = 120) -> bool:
        """这条采集记录是不是网关已经记过的同一笔请求。"""
        if not r.input_tokens and not r.output_tokens:
            return False
        row = self.conn.execute(
            """
            SELECT 1 FROM usage_records
            WHERE source = ?
              AND client_key IS NOT NULL
              AND input_tokens = ?
              AND output_tokens = ?
              AND cache_read = ?
              AND ABS(ts - ?) <= ?
            LIMIT 1
            """,
            (r.source, r.input_tokens, r.output_tokens, r.cache_read, r.ts, window_sec),
        ).fetchone()
        return row is not None

    # ---------- 额度 ----------

    def insert_quota(self, snaps: Iterable[QuotaSnapshot]) -> int:
        inserted = 0
        with self.lock:
            for s in snaps:
                self.conn.execute(
                    """
                    INSERT INTO quota_snapshots
                    (ts, source, account, window, remaining_pct, remaining_amt,
                     currency, resets_at, raw_json)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (s.ts, s.source, s.account, s.window, s.remaining_pct,
                     s.remaining_amt, s.currency, s.resets_at, dumps(s.raw)),
                )
                inserted += 1
            self.conn.commit()
        self._maybe_checkpoint()
        return inserted

    def prune_quota_snapshots(self, keep_days: int = 3) -> int:
        """额度快照只留最近几天。

        采集器每分钟写 81 行（4 个 Antigravity 账号 × 20 个窗口 + Mistral + Zen），
        一天就是 11 万行。留 3 天足够看趋势和判断阈值，再久纯属拖慢查询。
        """
        cutoff = int(time.time()) - keep_days * 86400
        with self.lock:
            cur = self.conn.execute("DELETE FROM quota_snapshots WHERE ts < ?", (cutoff,))
            self.conn.commit()
        return cur.rowcount

    # ---------- 追踪 ----------

    def insert_trace(
        self,
        *,
        trace: dict,
        ts: int,
        source: str | None = None,
        model: str | None = None,
        requested_model: str | None = None,
        client_key: str | None = None,
        status: int = 200,
    ) -> None:
        try:
            with self.lock:
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO request_traces
                    (trace_id, ts, source, model, requested_model, client_key,
                     status, total_ms, spans_json)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        trace.get("trace_id"), ts, source, model, requested_model,
                        client_key, status, trace.get("total_ms"), dumps(trace.get("spans")),
                    ),
                )
                self.conn.commit()
        except Exception:
            pass

    def record_request(
        self,
        *,
        usage_record: UsageRecord | None = None,
        trace: dict | None = None,
        ts: int,
        source: str | None = None,
        model: str | None = None,
        requested_model: str | None = None,
        client_key: str | None = None,
        status: int = 200,
    ) -> None:
        """单事务同时写入网关请求的用量与链路追踪，供后台线程调用。"""
        now = int(time.time())
        try:
            with self.lock:
                if usage_record is not None:
                    r = usage_record
                    self.conn.execute(
                        """
                        INSERT OR IGNORE INTO usage_records
                        (dedupe_key, ts, source, account, model, requested_model,
                         input_tokens, output_tokens, cache_read, cache_write, reasoning,
                         cost_usd, ttft_ms, latency_ms, status, client_key, raw_json, ingested_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            r.dedupe_key(), r.ts, r.source, r.account, r.model, r.requested_model,
                            r.input_tokens, r.output_tokens, r.cache_read, r.cache_write, r.reasoning,
                            r.cost_usd, r.ttft_ms, r.latency_ms, r.status, r.client_key,
                            dumps(r.raw), now,
                        ),
                    )
                if trace is not None:
                    self.conn.execute(
                        """
                        INSERT OR IGNORE INTO request_traces
                        (trace_id, ts, source, model, requested_model, client_key,
                         status, total_ms, spans_json)
                        VALUES (?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            trace.get("trace_id"), ts, source, model, requested_model,
                            client_key, status, trace.get("total_ms"), dumps(trace.get("spans")),
                        ),
                    )
                self.conn.commit()
            self._maybe_checkpoint()
        except Exception:
            pass

    def prune_traces(self, keep_days: int = 7) -> int:
        cutoff = int(time.time()) - keep_days * 86400
        with self.lock:
            cur = self.conn.execute("DELETE FROM request_traces WHERE ts < ?", (cutoff,))
            self.conn.commit()
        return cur.rowcount

    def prune_usage(self, keep_days: int = 365) -> int:
        """清理过期用量记录（默认保留一年），防止 usage_records 无限增长拖慢聚合查询。"""
        cutoff = int(time.time()) - keep_days * 86400
        with self.lock:
            cur = self.conn.execute("DELETE FROM usage_records WHERE ts < ?", (cutoff,))
            self.conn.commit()
        return cur.rowcount

    def backup_database(self, backup_dir: Path, keep: int = 7) -> Path | None:
        """每天滚动备份一次数据库（VACUUM INTO 生成紧凑的一致性快照）。

        返回本次备份路径；今天已备份过则返回 None。
        """
        today = time.strftime("%Y%m%d")
        target = Path(backup_dir) / f"usage-{today}.db"
        if target.exists():
            return None
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        with self.lock:
            tmp = target.with_suffix(".tmp.db")
            try:
                self.conn.execute("VACUUM INTO ?", (str(tmp),))
                tmp.replace(target)
            except Exception:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                return None
        # 只保留最近 keep 份
        backups = sorted(backup_dir.glob("usage-*.db"))
        for old in backups[:-keep] if len(backups) > keep else []:
            try:
                old.unlink()
            except OSError:
                pass
        return target

    # ---------- 水位线 ----------

    def watermark(self, name: str) -> int:
        row = self.read_conn().execute(
            "SELECT last_ts FROM collector_state WHERE collector = ?", (name,)
        ).fetchone()
        return int(row["last_ts"]) if row else 0

    def set_watermark(self, name: str, ts: int) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO collector_state (collector, last_ts, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(collector) DO UPDATE SET
                    last_ts = MAX(last_ts, excluded.last_ts),
                    updated_at = excluded.updated_at
                """,
                (name, ts, int(time.time())),
            )
            self.conn.commit()

    def close(self) -> None:
        with self.lock:
            for rc in list(self._read_conns):
                try:
                    rc.close()
                except Exception:
                    pass
            self._read_conns.clear()
            try:
                self.conn.execute("PRAGMA wal_checkpoint(FULL)")
                self.conn.commit()
            except Exception:
                pass
            try:
                self.conn.close()
            except Exception:
                pass
