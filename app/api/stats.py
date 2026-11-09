"""统计查询 API。"""
from __future__ import annotations

import time
import secrets
import re
from urllib.parse import unquote_to_bytes
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..config import is_loopback_host
from ..pricing import _multiplier_factor

def _matches_key(provided: str, expected: str) -> bool:
    # compare_digest on UTF-8 bytes also handles non-ASCII configured keys.
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _header_key_matches(request: Request, header: str, expected: str) -> bool:
    # A distinct header avoids ambiguity with existing literal ASCII secrets.
    encoded = request.headers.get(header + "-encoded")
    if encoded is not None:
        if re.fullmatch(r"(?:[A-Za-z0-9_.!~*'()-]|%[0-9A-Fa-f]{2})*", encoded) is None:
            return False
        try:
            provided = unquote_to_bytes(encoded).decode("utf-8", "strict")
        except UnicodeDecodeError:
            return False
        return _matches_key(provided, expected)
    return _matches_key(request.headers.get(header, ""), expected)


def _stats_guard(request: Request) -> None:
    # Management and diagnostics retain their own admin-only policy. Health is
    # deliberately minimal and public; readiness is also safe for orchestrators.
    # All other router endpoints default closed.
    route = request.scope.get("route")
    name = getattr(route, "name", "")
    if name in {
        "health", "readiness", "admin_checkpoint", "admin_shutdown",
        "diagnostics", "reset_upstream_health",
    }:
        return
    cfg = request.app.state.cfg
    expected = str(getattr(cfg, "stats_key", "") or "")
    if not expected and not is_loopback_host(getattr(cfg, "host", "127.0.0.1")):
        raise HTTPException(
            status_code=503,
            detail="stats API disabled: configure server.stats_key for a non-loopback bind",
        )
    if expected and not _header_key_matches(request, "x-stats-key", expected):
        raise HTTPException(status_code=401, detail="invalid stats key")


router = APIRouter(prefix="/api", tags=["stats"], dependencies=[Depends(_stats_guard)])

# Structured results take precedence; legacy rows fall back to HTTP status.
_SUCCESS = "(result = 'success' OR (result IS NULL AND status < 400))"
_FAILED = "((result IS NOT NULL AND result NOT IN ('success', 'client_cancelled', 'unknown')) OR (result IS NULL AND status >= 400))"
_CANCELLED = "(result = 'client_cancelled')"
_UNKNOWN = "(result = 'unknown' OR (result IS NULL AND status IS NULL))"
_INTERNAL_SOURCE = "gateway"
_HAS_USAGE = (
    "(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)"
    " + COALESCE(cache_read, 0) + COALESCE(cache_write, 0) > 0)"
)


def _add_source_filter(where: list[str], params: list[Any], source: str | None) -> None:
    """Filter to an explicit source or hide the gateway's internal error rows."""
    if source:
        where.append("source = ?")
        params.append(source)
    else:
        where.append("source <> ?")
        params.append(_INTERNAL_SOURCE)


def _bounds(range_: str) -> tuple[int, int]:
    now = int(time.time())
    if range_ == "today":
        start = int(datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    elif range_ in ("week", "7d"):
        start = now - 7 * 86400
    elif range_ in ("month", "30d"):
        start = now - 30 * 86400
    elif range_ == "all":
        start = 0
    elif range_ == "1d":
        start = now - 86400
    elif range_ == "3d":
        start = now - 3 * 86400
    else:
        start = now - 86400
    return start, now


def _store(request: Request):
    return request.app.state.store


def _billing_map(request: Request) -> dict[str, dict]:
    """source name -> {mode, monthly_fee, alias}"""
    cfg = request.app.state.cfg
    out = {}
    for up in cfg.upstreams:
        out[up.name] = {
            "mode": up.billing_mode,
            "monthly_fee": up.monthly_fee,
            "alias": up.prefix,
        }
    return out


def _split_cost(rows: list[dict], billing: dict[str, dict]) -> None:
    """给每个来源行补上计费模式标签。

    成本一律按「原价」（官方 API 价）计算并展示——
    订阅制/免费渠道只是不产生额外支出，但 token 本身的价值不变。
    """
    for r in rows:
        info = billing.get(r.get("source"), {"mode": "payg", "monthly_fee": 0.0, "alias": r.get("source")})
        mode = info["mode"]
        cost = r.get("cost") or 0
        r["billing_mode"] = mode
        r["alias"] = info["alias"]
        # 统一：实时价即原价
        r["cost_usd"] = cost
        r["real_cost_usd"] = cost
        r["equiv_cost_usd"] = cost
        if mode == "subscription":
            r["monthly_fee"] = info["monthly_fee"]


def _admin_guard(request: Request, *, allow_query: bool = True) -> JSONResponse | None:
    """管理端点鉴权：配置了 server.admin_key 时必须携带（x-admin-key 头或 admin_key 参数）。

    未配置时仅回环监听放行；非回环监听失败关闭，避免管理面裸奔。
    """
    cfg = request.app.state.cfg
    expected = str(getattr(cfg, "admin_key", "") or "")
    if not expected:
        if not is_loopback_host(getattr(cfg, "host", "127.0.0.1")):
            return JSONResponse(
                {
                    "error": {
                        "message": "management API disabled: configure server.admin_key "
                                   "for a non-loopback bind",
                        "type": "configuration_error",
                    }
                },
                status_code=503,
            )
        return None
    has_header = "x-admin-key-encoded" in request.headers or bool(request.headers.get("x-admin-key"))
    valid = (
        _header_key_matches(request, "x-admin-key", expected)
        if has_header
        else allow_query and _matches_key(request.query_params.get("admin_key", ""), expected)
    )
    if not valid:
        return JSONResponse(
            {"error": {"message": "invalid admin key", "type": "auth_error"}},
            status_code=401,
        )
    return None


@router.post("/admin/checkpoint")
def admin_checkpoint(request: Request):
    """强制把 WAL 合并进主库并 fsync。重启前调用可确保不丢数据。"""
    err = _admin_guard(request)
    if err:
        return err
    _store(request).checkpoint()
    return {"ok": True, "ts": int(time.time())}


@router.post("/admin/shutdown")
def admin_shutdown(request: Request):
    """优雅关闭：checkpoint 后让 uvicorn 退出。

    不在这里 close 数据库——交由 lifespan 的 finally 处理，
    否则正在处理的请求会遇到 closed database。
    """
    err = _admin_guard(request)
    if err:
        return err

    import threading

    store = _store(request)
    store.checkpoint()

    server = getattr(request.app.state, "server", None)

    def _exit():
        time.sleep(0.3)
        if server is not None:
            server.should_exit = True

    threading.Thread(target=_exit, daemon=True).start()
    return {"ok": True, "message": "shutting down", "server_aware": server is not None}


@router.get("/health")
def health(request: Request):
    return {"ok": True, "ts": int(time.time()), "version": "0.1.0"}


@router.get("/ready")
def readiness(request: Request):
    """Public liveness-with-dependencies probe for containers and proxies."""
    checks = {"database": "ok", "scheduler": "ok"}
    try:
        row = _store(request).read_conn().execute("SELECT 1").fetchone()
        if row is None or row[0] != 1:
            raise RuntimeError("database probe failed")
    except Exception:
        checks["database"] = "unavailable"

    scheduler = getattr(request.app.state, "sched", None)
    if scheduler is None or not bool(getattr(scheduler, "running", False)):
        checks["scheduler"] = "unavailable"

    ready = all(value == "ok" for value in checks.values())
    return JSONResponse(
        {"ok": ready, "ts": int(time.time()), "checks": checks},
        status_code=200 if ready else 503,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/upstreams/health")
def upstreams_health(request: Request):
    """每个上游的实时健康状态（失败次数、冷却剩余、成功率）。"""
    tracker = getattr(request.app.state, "tracker", None)
    cfg = request.app.state.cfg
    if tracker is None:
        return []

    result = []
    for snap in tracker.snapshot():
        up = cfg.find_by_prefix(snap["name"])
        result.append({
            **snap,
            "alias": up.prefix if up else snap["name"],
            "models": len(up.all_models) if up else 0,
            "priority": up.priority if up else 0,
            "weight": up.weight if up else 1,
        })
    return result


@router.post("/upstreams/{name}/reset")
def reset_upstream_health(request: Request, name: str):
    """手动清除某个上游的冷却状态。"""
    err = _admin_guard(request, allow_query=False)
    if err:
        return err
    tracker = getattr(request.app.state, "tracker", None)
    if tracker is None:
        return {"error": "tracker not available"}
    state = tracker.get(name)
    state.cooldown_until = 0.0
    state.consecutive_failures = 0
    return {"ok": True, "name": name, "state": state.to_dict()}


@router.get("/traces")
def traces(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    errors_only: bool = Query(False),
    slow_ms: int = Query(0, ge=0),
    source: str | None = None,
):
    """最近的请求链路追踪。"""
    conn = _store(request).read_conn()

    where = ["1=1"]
    params: list = []
    if errors_only:
        where.append(_FAILED)
    if slow_ms > 0:
        where.append("total_ms >= ?")
        params.append(slow_ms)
    if source:
        where.append("source = ?")
        params.append(source)

    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT trace_id, ts, source, model, requested_model, client_key,
               status, result, error_stage, error_kind, error_status, total_ms, spans_json
        FROM request_traces
        WHERE {' AND '.join(where)}
        ORDER BY ts DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

    out = []
    for r in rows:
        item = dict(r)
        from ..diagnostic_bundle import build_diagnostic_bundle
        safe = build_diagnostic_bundle([item])["traces"]
        item.pop("spans_json", None)
        item["spans"] = safe[0]["spans"] if safe else []
        out.append(item)
    return out


@router.get("/diagnostics")
def diagnostics(request: Request, limit: int = Query(50, ge=1, le=500)):
    """导出白名单诊断元数据；仍可能包含敏感时间信息，建议仅本机使用。"""
    from fastapi.responses import Response
    from ..diagnostic_bundle import build_diagnostic_bundle
    import json

    err = _admin_guard(request)
    if err:
        return err
    rows = _store(request).read_conn().execute(
        "SELECT ts, status, total_ms, spans_json FROM request_traces ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    content = json.dumps(build_diagnostic_bundle([dict(row) for row in rows]), ensure_ascii=False)
    return Response(content, media_type="application/json", headers={
        "Content-Disposition": 'attachment; filename="usage-gateway-diagnostics.json"',
        "Cache-Control": "no-store",
    })


@router.get("/traces/{trace_id}")
def trace_detail(request: Request, trace_id: str):
    conn = _store(request).read_conn()
    row = conn.execute(
        """SELECT id, trace_id, ts, source, model, requested_model, client_key,
                  status, result, error_stage, error_kind, error_status, total_ms, spans_json
           FROM request_traces WHERE trace_id = ?""", (trace_id,)
    ).fetchone()
    if not row:
        return {"error": "trace not found", "trace_id": trace_id}
    from ..diagnostic_bundle import build_diagnostic_bundle
    item = dict(row)
    safe = build_diagnostic_bundle([item])["traces"]
    item.pop("spans_json", None)
    item["spans"] = safe[0]["spans"] if safe else []
    return item


@router.get("/health-timeline")
def health_timeline(
    request: Request,
    minutes: int = Query(120, ge=10, le=10080),
    bucket_sec: int = Query(120, ge=60, le=3600),
):
    """请求健康时间线：按固定时间桶聚合成功/失败数。

    默认最近 120 分钟，每 2 分钟一个桶，共 60 个格子。
    返回的桶包含空桶（count=0），方便前端画完整的时间线。
    """
    now = int(time.time())
    return _health_timeline(request, minutes, bucket_sec, now - minutes * 60, now)


def _health_timeline(request: Request, minutes: int, bucket_sec: int, window_start: int, now: int):
    conn = _store(request).read_conn()
    # 对齐到桶边界
    start = window_start - (window_start % bucket_sec)

    rows = conn.execute(
        f"""
        SELECT (ts / ?) * ? AS bucket,
               COUNT(*) AS total,
               SUM(CASE WHEN {_SUCCESS} THEN 1 ELSE 0 END) AS ok,
               SUM(CASE WHEN {_FAILED} THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN {_CANCELLED} THEN 1 ELSE 0 END) AS cancelled,
               SUM(CASE WHEN {_UNKNOWN} THEN 1 ELSE 0 END) AS unknown,
               COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0)
                   + COALESCE(SUM(cache_read), 0) + COALESCE(SUM(cache_write), 0) AS tokens
        FROM usage_records
        WHERE ts BETWEEN ? AND ?
        GROUP BY bucket
        ORDER BY bucket
        """,
        (bucket_sec, bucket_sec, window_start, now),
    ).fetchall()

    by_bucket = {int(r["bucket"]): dict(r) for r in rows}

    result = []
    b = start
    while b <= now:
        item = by_bucket.get(b)
        total = item["total"] if item else 0
        ok = item["ok"] if item else 0
        failed = item["failed"] if item else 0
        local = _local_hour(b)
        result.append({
            "ts": b,
            "time": local.strftime("%m-%d %H:%M" if minutes >= 1440 else "%H:%M"),
            "bucket_offset": local.strftime("%z"),
            "partial": window_start > b or now < b + bucket_sec - 1,
            "total": total,
            "ok": ok,
            "failed": failed,
            "cancelled": item["cancelled"] if item else 0,
            "unknown": item["unknown"] if item else 0,
            "tokens": item["tokens"] if item else 0,
            # 4 means activity without a known success or failure.
            "level": (
                0 if total == 0
                else 4 if not (ok or failed)
                else 1 if failed and not ok
                else 2 if failed
                else 3
            ),
        })
        b += bucket_sec

    total_req = sum(r["total"] for r in result)
    total_fail = sum(r["failed"] for r in result)
    total_ok = sum(r["ok"] for r in result)
    active_buckets = sum(1 for r in result if r["total"] > 0)

    return {
        "minutes": minutes,
        "bucket_sec": bucket_sec,
        "start": start,
        "end": now,
        "buckets": result,
        "total_requests": total_req,
        "total_failed": total_fail,
        "total_ok": total_ok,
        "total_cancelled": sum(r["cancelled"] for r in result),
        "total_unknown": sum(r["unknown"] for r in result),
        "success_rate": round(total_ok / (total_ok + total_fail) * 100, 2) if total_ok + total_fail else None,
        "active_buckets": active_buckets,
    }


@router.get("/heatmap")
def heatmap(request: Request, days: int = Query(365, ge=7, le=730)):
    """最近 N 个服务端本地日历日的每日 token 用量。"""
    conn = _store(request).read_conn()
    now = int(time.time())
    start = _heatmap_start(now, days)

    rows = conn.execute(
        f"""
        SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS date,
               COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0)
                   + COALESCE(SUM(cache_read), 0) + COALESCE(SUM(cache_write), 0) AS tokens,
               COUNT(*) AS requests
        FROM usage_records
        WHERE ts BETWEEN ? AND ?
        GROUP BY date
        ORDER BY date
        """,
        (start, now),
    ).fetchall()

    return [dict(r) for r in rows]


def _heatmap_start(end: int, days: int) -> int:
    first_day = datetime.fromtimestamp(end) - timedelta(days=days - 1)
    return int(first_day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


@router.get("/summary")
def summary(request: Request, range_: str = Query("today", alias="range")):
    start, end = _bounds(range_)
    return _summary(request, range_, start, end)


def _summary(request: Request, range_: str, start: int, end: int):
    conn = _store(request).read_conn()

    speed_filter = f"({_SUCCESS}) AND latency_ms IS NOT NULL AND latency_ms > 0 AND output_tokens IS NOT NULL"
    rows = conn.execute(
        f"""
        SELECT source,
               COALESCE(SUM(input_tokens),0)  AS input,
               COALESCE(SUM(output_tokens),0) AS output,
               COALESCE(SUM(cache_read),0)    AS cache_read,
               COALESCE(SUM(cache_write),0)   AS cache_write,
               COALESCE(SUM(reasoning),0)     AS reasoning,
               COUNT(*)                       AS requests,
               COALESCE(SUM(cost_usd),0)      AS cost,
               SUM(CASE WHEN {_HAS_USAGE} AND cost_usd IS NOT NULL
                        THEN 1 ELSE 0 END) AS priced_requests,
               SUM(CASE WHEN {_HAS_USAGE} THEN 1 ELSE 0 END) AS billable_requests,
               SUM(CASE WHEN {_HAS_USAGE} AND cost_usd IS NULL
                        THEN 1 ELSE 0 END) AS unpriced_requests,
               SUM(CASE WHEN NOT {_HAS_USAGE} AND cost_usd IS NULL AND {_FAILED}
                        THEN 1 ELSE 0 END) AS failed_unbilled_requests,
               SUM(CASE WHEN source = '{_INTERNAL_SOURCE}' AND {_FAILED}
                        THEN 1 ELSE 0 END) AS gateway_entry_failures,
               SUM(CASE WHEN {_SUCCESS} THEN 1 ELSE 0 END) AS outcome_ok,
               SUM(CASE WHEN {_FAILED} THEN 1 ELSE 0 END) AS outcome_failed,
               SUM(CASE WHEN {_CANCELLED} THEN 1 ELSE 0 END) AS outcome_cancelled,
               SUM(CASE WHEN {_UNKNOWN} THEN 1 ELSE 0 END) AS outcome_unknown,
               SUM(CASE WHEN {speed_filter} THEN output_tokens ELSE 0 END) AS speed_tokens,
               SUM(CASE WHEN {speed_filter} THEN latency_ms ELSE 0 END) AS speed_ms,
               SUM(CASE WHEN {speed_filter} THEN 1 ELSE 0 END) AS speed_count
        FROM usage_records
        WHERE ts BETWEEN ? AND ?
        GROUP BY source
        ORDER BY input + output + cache_read + cache_write DESC
        """,
        (start, end),
    ).fetchall()

    by_source = [dict(r) for r in rows]
    ok = sum(s.pop('outcome_ok') for s in by_source)
    failed = sum(s.pop('outcome_failed') for s in by_source)
    cancelled = sum(s.pop('outcome_cancelled') for s in by_source)
    unknown = sum(s.pop('outcome_unknown') for s in by_source)
    billable = sum(s["billable_requests"] for s in by_source)
    unpriced = sum(s["unpriced_requests"] for s in by_source)
    failed_unbilled = sum(s["failed_unbilled_requests"] for s in by_source)
    gateway_entry_failures = sum(s["gateway_entry_failures"] for s in by_source)
    speed_tokens = speed_ms = speed_count = 0
    for s in by_source:
        tokens, ms, count = (s.pop(key) for key in ('speed_tokens', 'speed_ms', 'speed_count'))
        speed_tokens += tokens
        speed_ms += ms
        speed_count += count
        s['tok_per_sec'] = round(tokens / (ms / 1000), 2) if ms else 0
    billing = _billing_map(request)
    total_cost = sum(s["cost"] for s in by_source)
    _split_cost(by_source, billing)

    total_tokens = sum(s["input"] + s["output"] + s["cache_read"] + s["cache_write"] for s in by_source)
    total_requests = sum(s["requests"] for s in by_source)
    total_input = sum(s["input"] for s in by_source)
    total_cache_read = sum(s["cache_read"] for s in by_source)
    total_cache_write = sum(s["cache_write"] for s in by_source)
    cache_prompt = total_input + total_cache_read + total_cache_write
    # 成本统一按原价（官方 API 价）计
    real_cost = total_cost
    equiv_cost = total_cost
    priced = sum(s["priced_requests"] for s in by_source)

    # 订阅月费合计（每月固定支出，与请求量无关）
    monthly_total = round(sum(
        info["monthly_fee"] for info in billing.values()
        if info["mode"] == "subscription"
    ), 2)

    # 按客户端 key 拆
    key_rows = conn.execute(
        f"""
        SELECT COALESCE(client_key,'(direct)') AS client_key,
               client_key IS NULL AS is_collector,
               COUNT(*) AS requests,
               COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0)
                   + COALESCE(SUM(cache_read), 0) + COALESCE(SUM(cache_write), 0) AS tokens,
               COALESCE(SUM(cost_usd),0) AS cost,
               SUM(CASE WHEN {_HAS_USAGE} AND cost_usd IS NOT NULL
                        THEN 1 ELSE 0 END) AS priced_requests,
               SUM(CASE WHEN {_HAS_USAGE} THEN 1 ELSE 0 END) AS billable_requests,
               SUM(CASE WHEN {_HAS_USAGE} AND cost_usd IS NULL
                        THEN 1 ELSE 0 END) AS unpriced_requests,
               SUM(CASE WHEN NOT {_HAS_USAGE} AND cost_usd IS NULL AND {_FAILED}
                        THEN 1 ELSE 0 END) AS failed_unbilled_requests
        FROM usage_records
        WHERE ts BETWEEN ? AND ? AND source <> ?
        GROUP BY client_key
        ORDER BY tokens DESC
        """,
        (start, end, _INTERNAL_SOURCE),
    ).fetchall()

    overall_tps = round(speed_tokens / (speed_ms / 1000), 2) if speed_ms else 0

    span = max(1, end - start)

    # Local calendar days match the date grouping, including DST transitions.
    heat_end = datetime.fromtimestamp(end)
    heat_start = _heatmap_start(end, 365)
    heat_rows = conn.execute(
        """
        SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS date,
               COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0)
                   + COALESCE(SUM(cache_read), 0) + COALESCE(SUM(cache_write), 0) AS tokens
        FROM usage_records
        WHERE ts BETWEEN ? AND ?
        GROUP BY date
        ORDER BY date
        """,
        (heat_start, end),
    ).fetchall()

    return {
        "range": range_,
        "start": start,
        "end": end,
        "by_source": [s for s in by_source if s["source"] != _INTERNAL_SOURCE],
        "by_client_key": [dict(r) for r in key_rows],
        "heatmap": [dict(r) for r in heat_rows],
        "heatmap_end_date": heat_end.strftime("%Y-%m-%d"),
        "total_tokens": total_tokens,
        "total_requests": total_requests,
        "failed_requests": failed,
        "successful_requests": ok,
        "cancelled_requests": cancelled,
        "unknown_requests": unknown,
        "success_rate": round(ok / (ok + failed) * 100, 2) if ok + failed else None,
        "cache_rate": round(total_cache_read / cache_prompt * 100, 2) if cache_prompt else 0,
        "total_cost_usd": total_cost,
        "real_cost_usd": real_cost,
        "equiv_cost_usd": equiv_cost,
        "monthly_fee_usd": monthly_total,
        "priced_requests": priced,
        "billable_requests": billable,
        "unpriced_requests": unpriced,
        "failed_unbilled_requests": failed_unbilled,
        "gateway_entry_failures": gateway_entry_failures,
        "rpm": round(total_requests / (span / 60), 2),
        "tpm": round(total_tokens / (span / 60), 1),
        "tok_per_sec": overall_tps,
        "avg_latency_ms": round(speed_ms / speed_count, 1) if speed_count else 0,
    }


@router.get("/speed")
def speed(request: Request, limit: int = 20):
    """最近若干次请求的生成速度（tok/s），用于实时展示。"""
    conn = _store(request).read_conn()
    rows = conn.execute(
        f"""
        SELECT id, ts, source, model, input_tokens, output_tokens, cache_read, cache_write,
               reasoning, cost_usd, latency_ms, ttft_ms, status
        FROM usage_records
        WHERE {_SUCCESS} AND latency_ms IS NOT NULL AND latency_ms > 0 AND output_tokens IS NOT NULL
        ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()

    out = []
    for r in rows:
        item = dict(r)
        sec = r["latency_ms"] / 1000
        item["tok_per_sec"] = round(r["output_tokens"] / sec, 2) if sec > 0 else 0
        # 首 token 之后的纯生成速率（用户口径：始终按纯生成显示）
        if r["ttft_ms"] and r["latency_ms"] > r["ttft_ms"]:
            gen_sec = (r["latency_ms"] - r["ttft_ms"]) / 1000
            item["gen_tok_per_sec"] = round(r["output_tokens"] / gen_sec, 2) if gen_sec > 0 else 0
        else:
            item["gen_tok_per_sec"] = item["tok_per_sec"]
        out.append(item)

    latest = out[0] if out else None
    recent_tps = round(
        sum(x["output_tokens"] for x in out) / (sum(x["latency_ms"] for x in out) / 1000), 2
    ) if out else 0

    return {
        "latest": latest,
        "recent_tok_per_sec": recent_tps,
        "recent_count": len(out),
        "history": out,
    }


def _log_filter(start, end, source, model, client_key, status, origin, session_key=None, unassigned=False):
    where = ["ts BETWEEN ? AND ?"]
    params: list[Any] = [start, end]
    _add_source_filter(where, params, source)
    if client_key:
        where.append("client_key = ?")
        params.append(client_key)
    if model:
        bare = model.split("/", 1)[1] if "/" in model else model
        # 转义 LIKE 通配符，避免搜索词里的 % _ 被当成通配符
        esc = bare.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append(
            "(model LIKE ? ESCAPE '\\' OR requested_model LIKE ? ESCAPE '\\' "
            "OR model = ? OR requested_model = ?)"
        )
        like = f"%{esc}%"
        params.extend([like, like, model, model])
    if status == "success":
        where.append(_SUCCESS)
    elif status == "error":
        where.append(_FAILED)
    elif status == "cancelled":
        where.append(_CANCELLED)
    elif status == "unknown":
        where.append(_UNKNOWN)
    if origin == "gateway":
        where.append("client_key IS NOT NULL")
    elif origin == "collector":
        where.append("client_key IS NULL")

    if session_key is not None:
        where.append("session_key = ?")
        params.append(session_key)
    if unassigned:
        where.append("session_key IS NULL")
    return " AND ".join(where), params


@router.get("/sessions")
def sessions(
    request: Request,
    range_: str = Query("today", alias="range"),
    source: str | None = None,
    model: str | None = None,
    client_key: str | None = None,
    status: str = Query("all"),
    origin: str = Query("gateway"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """按匿名会话指纹汇总记录价格；NULL 单独归入未归属桶，不补算。"""
    start, end = _bounds(range_)
    cond, params = _log_filter(start, end, source, model, client_key, status, origin)
    conn = _store(request).read_conn()
    conn.execute("BEGIN")
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT session_key FROM usage_records WHERE {cond} GROUP BY session_key)",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT session_key, MIN(ts) AS first_ts, MAX(ts) AS last_ts,
                   COUNT(*) AS requests, COALESCE(SUM(input_tokens), 0) AS input,
                   COALESCE(SUM(output_tokens), 0) AS output,
                   COALESCE(SUM(cache_read), 0) AS cache_read,
                   COALESCE(SUM(cache_write), 0) AS cache_write,
                   COALESCE(SUM(cost_usd), 0) AS cost_recorded_usd,
                   SUM(CASE WHEN {_HAS_USAGE} THEN 1 ELSE 0 END) AS billable_requests,
                   SUM(CASE WHEN {_HAS_USAGE} AND cost_usd IS NULL
                            THEN 1 ELSE 0 END) AS unpriced_requests,
                   SUM(CASE WHEN NOT {_HAS_USAGE} AND cost_usd IS NULL AND {_FAILED}
                            THEN 1 ELSE 0 END) AS failed_unbilled_requests
            FROM usage_records WHERE {cond} GROUP BY session_key
            ORDER BY last_ts DESC, session_key ASC LIMIT ? OFFSET ?""",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
    finally:
        conn.rollback()
    return {"items": [dict(row) for row in rows], "total": total, "page": page,
            "page_size": page_size, "pages": (total + page_size - 1) // page_size,
            "cost_basis": "recorded_only"}


@router.get("/logs")
def logs(
    request: Request,
    range_: str = Query("today", alias="range"),
    source: str | None = None,
    model: str | None = None,
    client_key: str | None = None,
    status: str = Query("all"),
    origin: str = Query("gateway"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    session_key: str | None = None,
    unassigned: bool = False,
):
    """逐笔调用明细：每次请求的 token 构成、缓存命中率与单次费用。

    origin:
      gateway   —— 经过网关的实时调用（有 client_key / 延迟 / 链路追踪）
      collector —— 本地采集器同步的记录（Antigravity Tools、OpenCode 客户端日志等）
      all       —— 全部

    费用口径：
      - 优先用记录时的 cost_usd（上游实报或当时的价格表）；
      - 历史记录 cost_usd 为空的按当前价格表实时补算（上限 20000 条）；
      - breakdown 给出按当前价格表的分项拆解（单价 / 倍率 / 各项费用 / 缓存节省额）。
    """
    import json as _json

    cfg = request.app.state.cfg
    prices = getattr(cfg, "prices", None)
    conn = _store(request).read_conn()
    start, end = _bounds(range_)

    cond, params = _log_filter(start, end, source, model, client_key, status, origin, session_key, unassigned)

    # Fetch all database inputs from one snapshot; price calculations run after it ends.
    conn.execute("BEGIN")
    try:
        agg = conn.execute(
            f"""
            SELECT COUNT(*) AS requests,
                   COALESCE(SUM(input_tokens),0)  AS input,
                   COALESCE(SUM(output_tokens),0) AS output,
                   COALESCE(SUM(cache_read),0)    AS cache_read,
                   COALESCE(SUM(cache_write),0)   AS cache_write,
                   COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0)
                       + COALESCE(SUM(cache_read), 0) + COALESCE(SUM(cache_write), 0) AS tokens,
                   COALESCE(SUM(reasoning),0)     AS reasoning,
                   COALESCE(SUM(cost_usd),0)      AS cost_recorded,
                   SUM(CASE WHEN {_HAS_USAGE} THEN 1 ELSE 0 END) AS billable_requests,
                   SUM(CASE WHEN {_HAS_USAGE} AND cost_usd IS NOT NULL
                            THEN 1 ELSE 0 END) AS priced_billable,
                   SUM(CASE WHEN NOT {_HAS_USAGE} AND cost_usd IS NULL AND {_FAILED}
                            THEN 1 ELSE 0 END) AS failed_unbilled,
                   SUM(CASE WHEN source = '{_INTERNAL_SOURCE}' AND {_FAILED}
                            THEN 1 ELSE 0 END) AS gateway_entry_failures,
                   COALESCE(SUM(CASE WHEN {_FAILED} THEN 1 ELSE 0 END),0) AS failed,
                   COALESCE(SUM(CASE WHEN {_CANCELLED} THEN 1 ELSE 0 END),0) AS cancelled,
                   COALESCE(SUM(CASE WHEN {_UNKNOWN} THEN 1 ELSE 0 END),0) AS unknown
            FROM usage_records WHERE {cond}
            """,
            params,
        ).fetchone()

        unpriced_rows = conn.execute(
            f"""SELECT source, model, ts, input_tokens, output_tokens, cache_read, cache_write
                FROM usage_records WHERE {cond} AND cost_usd IS NULL
                  AND {_HAS_USAGE} LIMIT 20000""",
            params,
        ).fetchall() if prices is not None else []
        cache_rows = conn.execute(
            f"""SELECT source, model, (ts / 60) * 60 AS minute,
                       COALESCE(SUM(cache_read),0) AS cr, COUNT(*) AS requests
                FROM usage_records WHERE {cond} AND cache_read > 0 GROUP BY source, model, minute""",
            params,
        ).fetchall()
        rows = conn.execute(
            f"""SELECT id, ts, source, account, model, requested_model, client_key,
                       input_tokens, output_tokens, cache_read, cache_write, reasoning,
                       cost_usd, ttft_ms, latency_ms, status, raw_json,
                       result, error_stage, error_kind, error_status
                FROM usage_records WHERE {cond}
                ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?""",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
    finally:
        conn.rollback()

    # cost_usd 为空的记录按当前价格表补算
    cost_computed = 0.0
    null_costed = 0
    if prices is not None:
        for r in unpriced_rows:
            c = prices.compute(
                upstream=r["source"], model=r["model"],
                input_tokens=r["input_tokens"] or 0, output_tokens=r["output_tokens"] or 0,
                cache_read=r["cache_read"] or 0, cache_write=r["cache_write"] or 0,
                at=datetime.fromtimestamp(r["ts"], tz=timezone.utc),
            )
            if c is not None:
                cost_computed += c
                null_costed += 1

    # 缓存累计节省：命中部分按「未命中单价 - 缓存读单价」的差价计算；
    # 倍率规则精确到分钟，按 UTC 分钟分组后逐组取倍率。
    cache_saved = 0.0
    cache_priced_requests = 0
    cache_unpriced_requests = 0
    for r in cache_rows:
        price = prices.get(r["source"], r["model"]) if prices is not None else None
        if price is None:
            cache_unpriced_requests += r["requests"]
            continue
        cache_priced_requests += r["requests"]
        delta = max(0.0, price.input - price.cache_read)
        factor = _multiplier_factor(
            price.multipliers, datetime.fromtimestamp(r["minute"], tz=timezone.utc)
        )
        cache_saved += r["cr"] / 1_000_000 * delta * factor

    total = agg["requests"]
    billable = agg["billable_requests"] or 0
    priced_requests = (agg["priced_billable"] or 0) + null_costed
    unpriced_requests = max(0, billable - priced_requests)
    prompt_total = agg["input"] + agg["cache_read"] + agg["cache_write"]

    summary = {
        "requests": total,
        "failed": agg["failed"],
        "cancelled": agg["cancelled"],
        "unknown": agg["unknown"],
        "input": agg["input"],
        "output": agg["output"],
        "cache_read": agg["cache_read"],
        "cache_write": agg["cache_write"],
        "reasoning": agg["reasoning"],
        "prompt_tokens": prompt_total,
        "total_tokens": prompt_total + agg["output"],
        "cache_rate": round(agg["cache_read"] / prompt_total * 100, 2) if prompt_total else 0,
        "cost_usd": round((agg["cost_recorded"] or 0) + cost_computed, 6),
        "cost_recorded_usd": round(agg["cost_recorded"] or 0, 6),
        "cost_computed_usd": round(cost_computed, 6),
        "cache_saved_usd": round(cache_saved, 6),
        "cache_saved_priced_requests": cache_priced_requests,
        "cache_saved_unpriced_requests": cache_unpriced_requests,
        "priced_requests": priced_requests,
        "billable_requests": billable,
        "unpriced_requests": unpriced_requests,
        "failed_unbilled_requests": agg["failed_unbilled"] or 0,
        "gateway_entry_failures": agg["gateway_entry_failures"] or 0,
        "backfilled_requests": null_costed,
    }

    billing = _billing_map(request)
    items = []
    for r in rows:
        item = dict(r)
        try:
            raw = _json.loads(item.pop("raw_json") or "{}")
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}

        in_tok = item["input_tokens"] or 0
        out_tok = item["output_tokens"] or 0
        cr = item["cache_read"] or 0
        cw = item["cache_write"] or 0
        prompt = in_tok + cr + cw
        has_usage = in_tok + out_tok + cr + cw > 0

        info = billing.get(
            item["source"], {"mode": "payg", "monthly_fee": 0.0, "alias": item["source"]}
        )
        item["billing_mode"] = info["mode"]
        item["alias"] = info["alias"]

        item["prompt_tokens"] = prompt
        item["total_tokens"] = prompt + out_tok
        item["cache_rate"] = round(cr / prompt * 100, 1) if prompt else 0.0

        # 速度：与 /api/speed 同一口径（tok/s 与首字后的纯生成 tok/s）
        sec = (item["latency_ms"] or 0) / 1000
        item["tok_per_sec"] = round(out_tok / sec, 2) if sec > 0 else None
        # 纯生成速率（扣除首字），始终按纯生成显示
        gen_sec = ((item["latency_ms"] or 0) - (item["ttft_ms"] or 0)) / 1000
        if item["ttft_ms"] and gen_sec > 0:
            item["gen_tok_per_sec"] = round(out_tok / gen_sec, 2)
        else:
            item["gen_tok_per_sec"] = item["tok_per_sec"]

        # 分项拆解（按当前价格表）
        br = None
        if prices is not None and has_usage:
            br = prices.breakdown(
                upstream=item["source"], model=item["model"],
                input_tokens=in_tok, output_tokens=out_tok,
                cache_read=cr, cache_write=cw,
                at=datetime.fromtimestamp(item["ts"], tz=timezone.utc),
            )
        item["breakdown"] = br

        if item["cost_usd"] is not None:
            item["cost_source"] = "recorded"
        elif br is not None:
            item["cost_usd"] = br["total_cost"]
            item["cost_source"] = "computed"
        elif not has_usage:
            item["cost_source"] = "not_applicable"
        else:
            item["cost_source"] = "unpriced"

        item["estimated"] = bool(raw.get("estimated"))
        trace_id = raw.get("request_id")
        item["trace_id"] = trace_id if isinstance(trace_id, str) and trace_id else None
        items.append(item)

    pages = (total + page_size - 1) // page_size
    return {
        "items": items,
        "summary": summary,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.get("/logs/export")
def export_logs(
    request: Request,
    range_: str = Query("today", alias="range"),
    source: str | None = None,
    model: str | None = None,
    client_key: str | None = None,
    status: str = Query("all"),
    origin: str = Query("gateway"),
    session_key: str | None = None,
    unassigned: bool = False,
):
    """Read up to 5000 matching rows and the total from one snapshot."""
    return logs(request, range_, source, model, client_key, status, origin,
                1, 5000, session_key, unassigned)


@router.get("/usage")
def usage(
    request: Request,
    range_: str = Query("today", alias="range"),
    source: str | None = None,
    client_key: str | None = None,
    group_by: str = Query("model"),
):
    start, end = _bounds(range_)
    return _usage(request, start, end, source, client_key, group_by)


def _usage(request: Request, start: int, end: int, source: str | None,
           client_key: str | None, group_by: str):
    conn = _store(request).read_conn()

    col = {"model": "model", "source": "source", "account": "account",
           "client_key": "client_key"}.get(group_by, "model")

    where = ["ts BETWEEN ? AND ?"]
    params: list[Any] = [start, end]
    _add_source_filter(where, params, source)
    if client_key:
        where.append("client_key = ?")
        params.append(client_key)

    collector_field = "client_key IS NULL AS is_collector," if col == "client_key" else ""
    group_column = "client_key" if col == "client_key" else "grp"
    sql = f"""
        SELECT COALESCE({col},'(unknown)') AS grp,
               {collector_field}
               COALESCE(SUM(input_tokens),0)  AS input,
               COALESCE(SUM(output_tokens),0) AS output,
               COALESCE(SUM(cache_read),0)    AS cache_read,
               COALESCE(SUM(cache_write),0)   AS cache_write,
               COALESCE(SUM(reasoning),0)     AS reasoning,
               COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0)
                   + COALESCE(SUM(cache_read), 0) + COALESCE(SUM(cache_write), 0) AS tokens,
               COUNT(*)                       AS requests,
               COALESCE(SUM(cost_usd),0)      AS cost
        FROM usage_records
        WHERE {' AND '.join(where)}
        GROUP BY {group_column}
        ORDER BY tokens DESC
    """
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


@router.get("/timeseries")
def timeseries(
    request: Request,
    range_: str = Query("week", alias="range"),
    bucket: str = Query("day"),
    model: str | None = None,
    source: str | None = None,
):
    """按时间桶聚合的用量和成本。

    bucket: "hour" | "day"
    model:  可选，只看某个模型（支持带前缀写法）
    """
    start, end = _bounds(range_)
    return _timeseries(request, range_, bucket, model, source, start, end)


def _timeseries(request: Request, range_: str, bucket: str, model: str | None,
                source: str | None, start: int, end: int):
    conn = _store(request).read_conn()
    fmt = "%Y-%m-%d %H:%M" if bucket == "hour" else "%Y-%m-%d"
    if range_ == "all":
        earliest = conn.execute("SELECT MIN(ts) FROM usage_records WHERE ts BETWEEN ? AND ?", (start, end)).fetchone()[0]
        start = earliest if earliest is not None else end

    where = ["ts BETWEEN ? AND ?"]
    params: list[Any] = [start, end]
    if model:
        # 支持 "opgo/kimi-k3" 或纯 "kimi-k3"
        if source:
            where.append("model = ?")
            params.append(model)
        else:
            bare = model.split("/", 1)[1] if "/" in model else model
            where.append("(model = ? OR requested_model = ? OR model = ?)")
            params.extend([bare, model, model])
    _add_source_filter(where, params, source)

    bucket_expr = _bucket_expression(bucket, start)
    rows = conn.execute(
        f"""
        SELECT {bucket_expr} AS bucket,
               source,
               COALESCE(SUM(input_tokens),0)  AS input,
               COALESCE(SUM(output_tokens),0) AS output,
               COALESCE(SUM(cache_read),0)    AS cache_read,
               COALESCE(SUM(cache_write),0)   AS cache_write,
               COALESCE(SUM(reasoning),0)     AS reasoning,
               COUNT(*)                       AS requests,
               COALESCE(SUM(cost_usd),0)      AS cost,
               SUM(CASE WHEN {_HAS_USAGE} AND cost_usd IS NOT NULL
                        THEN 1 ELSE 0 END) AS priced_requests,
               SUM(CASE WHEN {_HAS_USAGE} THEN 1 ELSE 0 END) AS billable_requests,
               SUM(CASE WHEN {_HAS_USAGE} AND cost_usd IS NULL
                        THEN 1 ELSE 0 END) AS unpriced_requests
        FROM usage_records
        WHERE {' AND '.join(where)}
        GROUP BY bucket, source
        ORDER BY bucket
        """,
        params,
    ).fetchall()

    merged: dict[str, dict[str, Any]] = {}
    for r in rows:
        b = r["bucket"]
        item = merged.setdefault(
            b, {"bucket": b, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                "reasoning": 0, "requests": 0, "priced_requests": 0,
                "billable_requests": 0, "unpriced_requests": 0,
                "cost": 0.0, "by_source": {}}
        )
        for k in ("input", "output", "cache_read", "cache_write", "reasoning", "requests",
                  "priced_requests", "billable_requests", "unpriced_requests"):
            item[k] += r[k]
        item["cost"] += r["cost"] or 0
        item["by_source"][r["source"]] = r["input"] + r["output"] + r["cache_read"] + r["cache_write"]

    # Hour buckets use elapsed time so repeated or skipped local hours stay distinct.
    if bucket == "hour":
        result = []
        for timestamp in range(_hour_bucket_start(start), end + 1, 3600):
            v = merged.get(timestamp, {"input": 0, "output": 0, "cache_read": 0,
                                       "cache_write": 0, "reasoning": 0, "requests": 0,
                                       "priced_requests": 0, "billable_requests": 0,
                                       "unpriced_requests": 0, "cost": 0.0, "by_source": {}})
            local = _local_hour(timestamp)
            v.update(bucket=local.strftime(fmt), bucket_start=timestamp,
                     bucket_offset=local.strftime("%z"),
                     partial=start > timestamp or end < timestamp + 3599)
            result.append(v)
        return result

    # Day buckets follow local calendar dates.
    cursor = datetime.fromtimestamp(start).replace(minute=0, second=0, microsecond=0)
    if bucket != "hour":
        cursor = cursor.replace(hour=0)
    stop = datetime.fromtimestamp(end)
    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    result = []
    while cursor <= stop:
        key = cursor.strftime(fmt)
        v = merged.get(key, {"bucket": key, "input": 0, "output": 0, "cache_read": 0,
                             "cache_write": 0, "reasoning": 0, "requests": 0,
                             "priced_requests": 0, "billable_requests": 0,
                             "unpriced_requests": 0, "cost": 0.0, "by_source": {}})
        result.append(v)
        cursor += step
    return result


def _hour_bucket_start(start: int) -> int:
    local = _local_hour(start)
    return start - local.minute * 60 - local.second


def _local_hour(timestamp: int) -> datetime:
    return datetime.fromtimestamp(timestamp, timezone.utc).astimezone()


def _bucket_expression(bucket: str, start: int) -> str:
    if bucket == "hour":
        anchor = _hour_bucket_start(start)
        return f"({anchor} + ((ts - {anchor}) / 3600) * 3600)"
    return "strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime')"


@router.get("/models/active")
def active_models(request: Request, range_: str = Query("today", alias="range")):
    """当前时间范围内有请求的模型列表，用于图表线条选择。"""
    start, end = _bounds(range_)
    return _active_models(request, start, end)


def _active_models(request: Request, start: int, end: int):
    conn = _store(request).read_conn()
    rows = conn.execute(
        f"""
        SELECT source, model, COUNT(*) AS requests,
               COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0)
                   + COALESCE(SUM(cache_read), 0) + COALESCE(SUM(cache_write), 0) AS tokens
        FROM usage_records
        WHERE ts BETWEEN ? AND ? AND source <> ?
        GROUP BY source, model
        ORDER BY tokens DESC, source, model
        """,
        (start, end, _INTERNAL_SOURCE),
    ).fetchall()
    return [dict(r) for r in rows]


def _model_timeseries(request: Request, bucket: str, start: int, end: int):
    bucket_expr = _bucket_expression(bucket, start)
    rows = _store(request).read_conn().execute(
        f"""
        SELECT {bucket_expr} AS bucket,
               source, model, COUNT(*) AS requests,
               COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0)
                   + COALESCE(SUM(cache_read), 0) + COALESCE(SUM(cache_write), 0) AS tokens
        FROM usage_records
        WHERE ts BETWEEN ? AND ? AND source <> ?
        GROUP BY bucket, source, model
        ORDER BY bucket, source, model
        """,
        (start, end, _INTERNAL_SOURCE),
    ).fetchall()
    result = [dict(r) for r in rows]
    if bucket == "hour":
        for row in result:
            timestamp = row["bucket"]
            local = _local_hour(timestamp)
            row.update(bucket=local.strftime("%Y-%m-%d %H:%M"),
                       bucket_start=timestamp, bucket_offset=local.strftime("%z"))
    return result


@router.get("/quota")
def quota(request: Request):
    from ..scheduler import load_quota_items

    from ..alerts import item_level_name

    cfg = request.app.state.cfg.alerts
    out = []
    for it in load_quota_items(_store(request), cfg.stale_sec):
        d = it.to_dict()
        d["level"] = item_level_name(it, cfg)
        out.append(d)
    return out


@router.get("/alerts")
def alerts(request: Request):
    """额度告警视图：当前每条额度的等级 + 最近提醒记录。

    等级只看剩余百分比：<= critical_pct 是 critical，<= warn_pct 是 warn。
    数据过期（采集器挂了）的窗口标记 stale，不参与判断。
    """
    alerter = getattr(request.app.state, "alerter", None)
    if alerter is None:
        return {"enabled": False, "items": [], "counts": {}, "history": []}

    from ..scheduler import load_quota_items

    items = load_quota_items(_store(request), alerter.cfg.stale_sec)
    view = alerter.snapshot(items)
    view["enabled"] = True
    return view


@router.get("/balance")
def balance(request: Request):
    """给外部监控工具用的简单余额接口。"""
    conn = _store(request).read_conn()
    rows = conn.execute(
        """
        SELECT source, remaining_amt, currency
        FROM quota_snapshots
        WHERE window = 'balance' AND remaining_amt IS NOT NULL
          AND ts = (SELECT MAX(ts) FROM quota_snapshots q2
                    WHERE q2.source = quota_snapshots.source
                      AND q2.window = 'balance')
        """
    ).fetchall()
    total = sum(r["remaining_amt"] for r in rows if (r["currency"] or "USD") == "USD")
    return {
        "balance": round(total, 4),
        "currency": "USD",
        "detail": [dict(r) for r in rows],
    }


@router.get("/models")
def models(request: Request):
    """网关对外暴露的模型列表（OpenAI 格式）。

    只返回带前缀的模型，强制客户端明确指定上游：
        anti/gemini-3.8-flash
        mistral/mistral-large-latest
        opgo/kimi-k3
        axon/gemma-4-31b-it

    不可用的模型（探测失败）不会出现在列表里。
    """
    cfg = request.app.state.cfg
    data = []
    for up in cfg.upstreams:
        for m in up.all_models:
            data.append({
                "id": f"{up.prefix}/{m}",
                "object": "model",
                "owned_by": up.name,
                "upstream": up.name,
                "canonical_id": m,
            })
    return {"object": "list", "data": data}


@router.get("/routes")
def routes(request: Request):
    cfg = request.app.state.cfg
    return [
        {
            "name": up.name,
            "alias": up.prefix,
            "type": up.type,
            "base_url": up.base_url,
            "models": up.all_models,
            "has_key": bool(up.api_key),
            "priced_models": len((cfg.prices.table.get(up.name) or {})) if cfg.prices else 0,
        }
        for up in cfg.upstreams
    ]


@router.get("/pricing")
def pricing(request: Request, upstream: str | None = None):
    """返回已加载的价格表，便于核对。"""
    cfg = request.app.state.cfg
    if cfg.prices is None:
        return {"error": "pricing not loaded"}

    result: dict[str, dict] = {}
    for up_name, models in cfg.prices.table.items():
        if upstream and up_name != upstream:
            continue
        result[up_name] = {
            model: {
                "input": p.input,
                "output": p.output,
                "cache_read": p.cache_read,
                "cache_write": p.cache_write,
                "has_multipliers": bool(p.multipliers),
            }
            for model, p in models.items()
        }
    return result


@router.get("/costs")
def costs(request: Request, range_: str = Query("today", alias="range")):
    """按上游 × 模型拆分的成本明细。"""
    start, end = _bounds(range_)
    return _costs(request, start, end)


def _costs(request: Request, start: int, end: int):
    conn = _store(request).read_conn()

    rows = conn.execute(
        f"""
        SELECT source, model,
               COUNT(*) AS requests,
               COALESCE(SUM(input_tokens),0) AS input,
               COALESCE(SUM(output_tokens),0) AS output,
               COALESCE(SUM(cache_read),0) AS cache_read,
               COALESCE(SUM(cost_usd),0) AS cost,
               SUM(CASE WHEN {_HAS_USAGE} AND cost_usd IS NOT NULL
                        THEN 1 ELSE 0 END) AS priced,
               SUM(CASE WHEN {_HAS_USAGE} THEN 1 ELSE 0 END) AS billable_requests,
               SUM(CASE WHEN {_HAS_USAGE} AND cost_usd IS NULL
                        THEN 1 ELSE 0 END) AS unpriced_requests
        FROM usage_records
        WHERE ts BETWEEN ? AND ? AND source <> ?
        GROUP BY source, model
        ORDER BY cost DESC, input + output DESC, source, model
        """,
        (start, end, _INTERNAL_SOURCE),
    ).fetchall()

    return _format_costs(request, rows)


def _format_costs(request: Request, rows):

    out = []
    billing = _billing_map(request)
    for r in rows:
        item = dict(r)
        item["cost"] = item["cost"] or 0
        item["avg_cost_per_request"] = (
            item["cost"] / item["requests"] if item["requests"] else 0
        )
        info = billing.get(item["source"], {"mode": "payg", "alias": item["source"]})
        item["billing_mode"] = info["mode"]
        item["alias"] = info["alias"]
        out.append(item)
    return out


def _overview_model_breakdown(request: Request, start: int, end: int):
    rows = _store(request).read_conn().execute(
        f"""
        SELECT source, model, COUNT(*) AS requests,
               COALESCE(SUM(input_tokens), 0) AS input,
               COALESCE(SUM(output_tokens), 0) AS output,
               COALESCE(SUM(cache_read), 0) AS cache_read,
               COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0)
                   + COALESCE(SUM(cache_read), 0) + COALESCE(SUM(cache_write), 0) AS tokens,
               COALESCE(SUM(cost_usd), 0) AS cost,
               SUM(CASE WHEN {_HAS_USAGE} AND cost_usd IS NOT NULL
                        THEN 1 ELSE 0 END) AS priced,
               SUM(CASE WHEN {_HAS_USAGE} THEN 1 ELSE 0 END) AS billable_requests,
               SUM(CASE WHEN {_HAS_USAGE} AND cost_usd IS NULL
                        THEN 1 ELSE 0 END) AS unpriced_requests
        FROM usage_records
        WHERE ts BETWEEN ? AND ? AND source <> ?
        GROUP BY source, model
        ORDER BY source, model
        """, (start, end, _INTERNAL_SOURCE),
    ).fetchall()
    active = [dict(source=r["source"], model=r["model"], requests=r["requests"],
                   tokens=r["tokens"]) for r in rows]
    active.sort(key=lambda item: item["tokens"], reverse=True)
    cost_rows = [dict(source=r["source"], model=r["model"], requests=r["requests"],
                      input=r["input"], output=r["output"], cache_read=r["cache_read"],
                      cost=r["cost"], priced=r["priced"],
                      billable_requests=r["billable_requests"],
                      unpriced_requests=r["unpriced_requests"]) for r in rows]
    cost_rows.sort(key=lambda item: (item["cost"], item["input"] + item["output"]), reverse=True)
    return active, _format_costs(request, cost_rows)


@router.get("/overview-usage")
def overview_usage(request: Request, range_: str = Query("today", alias="range")):
    """Read related dashboard aggregates from one SQLite snapshot and time range."""
    start, end = _bounds(range_)
    bucket = "hour" if range_ in ("today", "1d", "3d", "7d") else "day"
    conn = _store(request).read_conn()
    conn.execute("BEGIN")
    try:
        active_models, costs = _overview_model_breakdown(request, start, end)
        result = {
            "summary": _summary(request, range_, start, end),
            "timeseries": _timeseries(request, range_, bucket, None, None, start, end),
            "models": _usage(request, start, end, None, None, "model"),
            "active_models": active_models,
            "model_timeseries": _model_timeseries(request, bucket, start, end),
            "costs": costs,
        }
        health_ranges = {"1d": (1440, 120), "3d": (4320, 1200), "7d": (10080, 1800)}
        if range_ in health_ranges:
            minutes, bucket_sec = health_ranges[range_]
            result["health_timeline"] = _health_timeline(request, minutes, bucket_sec, start, end)
        return result
    finally:
        conn.rollback()
