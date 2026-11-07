"""统计查询 API。"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from ..pricing import _multiplier_factor

router = APIRouter(prefix="/api", tags=["stats"])


def _bounds(range_: str) -> tuple[int, int]:
    now = int(time.time())
    if range_ == "today":
        start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    elif range_ in ("week", "7d"):
        start = now - 7 * 86400
    elif range_ in ("month", "30d"):
        start = now - 30 * 86400
    elif range_ == "all":
        start = 0
    elif range_ == "1d":
        start = now - 86400
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
        cost = round(r.get("cost") or 0, 6)
        r["billing_mode"] = mode
        r["alias"] = info["alias"]
        # 统一：实时价即原价
        r["cost_usd"] = cost
        r["real_cost_usd"] = cost
        r["equiv_cost_usd"] = cost
        if mode == "subscription":
            r["monthly_fee"] = info["monthly_fee"]


def _admin_guard(request: Request) -> JSONResponse | None:
    """管理端点鉴权：配置了 server.admin_key 时必须携带（x-admin-key 头或 admin_key 参数）。

    未配置则放行 —— 仅绑 127.0.0.1 的本机部署可接受；改 host 为 0.0.0.0 时务必配置。
    """
    expected = str(getattr(request.app.state.cfg, "admin_key", "") or "")
    if not expected:
        return None
    provided = request.headers.get("x-admin-key") or request.query_params.get("admin_key") or ""
    if provided != expected:
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
    import json as _json
    conn = _store(request).read_conn()

    where = ["1=1"]
    params: list = []
    if errors_only:
        where.append("status >= 400")
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
               status, total_ms, spans_json
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
        try:
            item["spans"] = _json.loads(item.pop("spans_json") or "[]")
        except Exception:
            item["spans"] = []
            item.pop("spans_json", None)
        out.append(item)
    return out


@router.get("/traces/{trace_id}")
def trace_detail(request: Request, trace_id: str):
    import json as _json
    conn = _store(request).read_conn()
    row = conn.execute(
        "SELECT * FROM request_traces WHERE trace_id = ?", (trace_id,)
    ).fetchone()
    if not row:
        return {"error": "trace not found", "trace_id": trace_id}
    item = dict(row)
    try:
        item["spans"] = _json.loads(item.pop("spans_json") or "[]")
    except Exception:
        item["spans"] = []
        item.pop("spans_json", None)
    return item


@router.get("/health-timeline")
def health_timeline(
    request: Request,
    minutes: int = Query(120, ge=10, le=1440),
    bucket_sec: int = Query(120, ge=60, le=3600),
):
    """请求健康时间线：按固定时间桶聚合成功/失败数。

    默认最近 120 分钟，每 2 分钟一个桶，共 60 个格子。
    返回的桶包含空桶（count=0），方便前端画完整的时间线。
    """
    conn = _store(request).read_conn()
    now = int(time.time())
    start = now - minutes * 60
    # 对齐到桶边界
    start = start - (start % bucket_sec)

    rows = conn.execute(
        """
        SELECT (ts / ?) * ? AS bucket,
               COUNT(*) AS total,
               SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS failed,
               COALESCE(SUM(input_tokens + output_tokens + cache_read), 0) AS tokens
        FROM usage_records
        WHERE ts >= ?
        GROUP BY bucket
        ORDER BY bucket
        """,
        (bucket_sec, bucket_sec, start),
    ).fetchall()

    by_bucket = {int(r["bucket"]): dict(r) for r in rows}

    result = []
    b = start
    while b <= now:
        item = by_bucket.get(b)
        total = item["total"] if item else 0
        failed = item["failed"] if item else 0
        result.append({
            "ts": b,
            "time": datetime.fromtimestamp(b).strftime("%H:%M"),
            "total": total,
            "ok": total - failed,
            "failed": failed,
            "tokens": item["tokens"] if item else 0,
            # 健康度：0=无请求, 1=全失败, 2=部分失败, 3=全成功
            "level": (
                0 if total == 0
                else 1 if failed == total
                else 2 if failed > 0
                else 3
            ),
        })
        b += bucket_sec

    total_req = sum(r["total"] for r in result)
    total_fail = sum(r["failed"] for r in result)
    active_buckets = sum(1 for r in result if r["total"] > 0)

    return {
        "minutes": minutes,
        "bucket_sec": bucket_sec,
        "start": start,
        "end": now,
        "buckets": result,
        "total_requests": total_req,
        "total_failed": total_fail,
        "success_rate": round((total_req - total_fail) / total_req * 100, 2) if total_req else 0,
        "active_buckets": active_buckets,
    }


@router.get("/heatmap")
def heatmap(request: Request, days: int = Query(365, ge=7, le=730)):
    """最近 N 天的每日 token 用量，用于热力图。"""
    conn = _store(request).read_conn()
    now = int(time.time())
    start = now - days * 86400

    rows = conn.execute(
        """
        SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS date,
               COALESCE(SUM(input_tokens + output_tokens + cache_read), 0) AS tokens,
               COUNT(*) AS requests
        FROM usage_records
        WHERE ts >= ?
        GROUP BY date
        ORDER BY date
        """,
        (start,),
    ).fetchall()

    return [dict(r) for r in rows]


@router.get("/summary")
def summary(request: Request, range_: str = Query("today", alias="range")):
    start, end = _bounds(range_)
    conn = _store(request).read_conn()

    rows = conn.execute(
        """
        SELECT source,
               COALESCE(SUM(input_tokens),0)  AS input,
               COALESCE(SUM(output_tokens),0) AS output,
               COALESCE(SUM(cache_read),0)    AS cache_read,
               COALESCE(SUM(reasoning),0)     AS reasoning,
               COUNT(*)                       AS requests,
               COALESCE(SUM(cost_usd),0)      AS cost,
               SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS priced_requests
        FROM usage_records
        WHERE ts BETWEEN ? AND ?
        GROUP BY source
        ORDER BY input + output DESC
        """,
        (start, end),
    ).fetchall()

    by_source = [dict(r) for r in rows]
    billing = _billing_map(request)
    _split_cost(by_source, billing)

    total_tokens = sum(s["input"] + s["output"] + s["cache_read"] for s in by_source)
    total_requests = sum(s["requests"] for s in by_source)
    total_input = sum(s["input"] for s in by_source)
    total_cache = sum(s["cache_read"] for s in by_source)
    # 成本统一按原价（官方 API 价）计
    total_cost = round(sum(s["cost_usd"] for s in by_source), 6)
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
        """
        SELECT COALESCE(client_key,'(direct)') AS client_key,
               COUNT(*) AS requests,
               COALESCE(SUM(input_tokens+output_tokens+cache_read),0) AS tokens,
               COALESCE(SUM(cost_usd),0) AS cost
        FROM usage_records
        WHERE ts BETWEEN ? AND ?
        GROUP BY client_key
        ORDER BY tokens DESC
        """,
        (start, end),
    ).fetchall()

    # 失败请求数
    failed = conn.execute(
        "SELECT COUNT(*) FROM usage_records WHERE ts BETWEEN ? AND ? AND status >= 400",
        (start, end),
    ).fetchone()[0]

    # 生成速度：只统计成功且有延迟的记录
    speed = conn.execute(
        """
        SELECT COALESCE(SUM(output_tokens),0) AS out_tokens,
               COALESCE(SUM(latency_ms),0)    AS total_ms,
               COUNT(*)                       AS n
        FROM usage_records
        WHERE ts BETWEEN ? AND ? AND status < 400
          AND latency_ms IS NOT NULL AND latency_ms > 0
        """,
        (start, end),
    ).fetchone()

    speed_by_source = conn.execute(
        """
        SELECT source,
               COALESCE(SUM(output_tokens),0) AS out_tokens,
               COALESCE(SUM(latency_ms),0)    AS total_ms,
               COUNT(*)                       AS n
        FROM usage_records
        WHERE ts BETWEEN ? AND ? AND status < 400
          AND latency_ms IS NOT NULL AND latency_ms > 0
        GROUP BY source
        """,
        (start, end),
    ).fetchall()

    speed_map = {
        r["source"]: round(r["out_tokens"] / (r["total_ms"] / 1000), 2) if r["total_ms"] else 0
        for r in speed_by_source
    }
    for s in by_source:
        s["tok_per_sec"] = speed_map.get(s["source"], 0)

    overall_tps = round(speed["out_tokens"] / (speed["total_ms"] / 1000), 2) if speed["total_ms"] else 0

    span = max(1, end - start)

    # 热力图数据（最近 365 天）
    heat_rows = conn.execute(
        """
        SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS date,
               COALESCE(SUM(input_tokens + output_tokens + cache_read), 0) AS tokens
        FROM usage_records
        WHERE ts >= ?
        GROUP BY date
        ORDER BY date
        """,
        (int(time.time()) - 365 * 86400,),
    ).fetchall()

    return {
        "range": range_,
        "start": start,
        "end": end,
        "by_source": by_source,
        "by_client_key": [dict(r) for r in key_rows],
        "heatmap": [dict(r) for r in heat_rows],
        "total_tokens": total_tokens,
        "total_requests": total_requests,
        "failed_requests": failed,
        "success_rate": round((total_requests - failed) / total_requests * 100, 2) if total_requests else 0,
        "cache_rate": round(total_cache / (total_input + total_cache) * 100, 2) if (total_input + total_cache) else 0,
        "total_cost_usd": total_cost,
        "real_cost_usd": real_cost,
        "equiv_cost_usd": equiv_cost,
        "monthly_fee_usd": monthly_total,
        "priced_requests": priced,
        "unpriced_requests": total_requests - priced,
        "rpm": round(total_requests / (span / 60), 2),
        "tpm": round(total_tokens / (span / 60), 1),
        "tok_per_sec": overall_tps,
        "avg_latency_ms": round(speed["total_ms"] / speed["n"], 1) if speed["n"] else 0,
    }


@router.get("/speed")
def speed(request: Request, limit: int = 20):
    """最近若干次请求的生成速度（tok/s），用于实时展示。"""
    conn = _store(request).read_conn()
    rows = conn.execute(
        """
        SELECT ts, source, model, input_tokens, output_tokens, cache_read, cache_write,
               reasoning, cost_usd, latency_ms, ttft_ms, status
        FROM usage_records
        WHERE status < 400 AND latency_ms IS NOT NULL AND latency_ms > 0
        ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()

    out = []
    for r in rows:
        item = dict(r)
        sec = r["latency_ms"] / 1000
        item["tok_per_sec"] = round(r["output_tokens"] / sec, 2) if sec > 0 else 0
        # 首 token 之后的速度（更接近真实生成速率）
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

    where = ["ts BETWEEN ? AND ?"]
    params: list[Any] = [start, end]
    if source:
        where.append("source = ?")
        params.append(source)
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
        where.append("status < 400")
    elif status == "error":
        where.append("status >= 400")
    if origin == "gateway":
        where.append("client_key IS NOT NULL")
    elif origin == "collector":
        where.append("client_key IS NULL")

    cond = " AND ".join(where)

    # ---- 汇总（当前筛选全集）----
    agg = conn.execute(
        f"""
        SELECT COUNT(*) AS requests,
               COALESCE(SUM(input_tokens),0)  AS input,
               COALESCE(SUM(output_tokens),0) AS output,
               COALESCE(SUM(cache_read),0)    AS cache_read,
               COALESCE(SUM(cache_write),0)   AS cache_write,
               COALESCE(SUM(reasoning),0)     AS reasoning,
               COALESCE(SUM(cost_usd),0)      AS cost_recorded,
               SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS priced_count,
               COALESCE(SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END),0) AS failed
        FROM usage_records WHERE {cond}
        """,
        params,
    ).fetchone()

    # cost_usd 为空的记录按当前价格表补算
    cost_computed = 0.0
    null_costed = 0
    if prices is not None:
        for r in conn.execute(
            f"""
            SELECT source, model, ts, input_tokens, output_tokens, cache_read, cache_write
            FROM usage_records WHERE {cond} AND cost_usd IS NULL
            LIMIT 20000
            """,
            params,
        ).fetchall():
            c = prices.compute(
                upstream=r["source"], model=r["model"],
                input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
                cache_read=r["cache_read"], cache_write=r["cache_write"],
                at=datetime.fromtimestamp(r["ts"], tz=timezone.utc),
            )
            if c is not None:
                cost_computed += c
                null_costed += 1

    # 缓存累计节省：命中部分按「未命中单价 - 缓存读单价」的差价计算；
    # 时段倍率会变化，按 (source, model, 小时桶) 分组后逐组取倍率（小时边界内倍率恒定）
    cache_saved = 0.0
    if prices is not None:
        for r in conn.execute(
            f"""
            SELECT source, model, (ts / 3600) * 3600 AS hr,
                   COALESCE(SUM(cache_read),0) AS cr
            FROM usage_records WHERE {cond}
            GROUP BY source, model, hr
            """,
            params,
        ).fetchall():
            price = prices.get(r["source"], r["model"])
            if price is None or r["cr"] <= 0:
                continue
            delta = max(0.0, price.input - price.cache_read)
            if delta <= 0:
                continue
            factor = _multiplier_factor(
                price.multipliers, datetime.fromtimestamp(r["hr"], tz=timezone.utc)
            )
            cache_saved += r["cr"] / 1_000_000 * delta * factor

    total = agg["requests"]
    total_null_cost = total - (agg["priced_count"] or 0)
    prompt_total = agg["input"] + agg["cache_read"] + agg["cache_write"]

    summary = {
        "requests": total,
        "failed": agg["failed"],
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
        "priced_requests": total - (total_null_cost - null_costed),
        "unpriced_requests": total_null_cost - null_costed,
        "backfilled_requests": null_costed,
    }

    # ---- 分页明细 ----
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"""
        SELECT id, ts, source, account, model, requested_model, client_key,
               input_tokens, output_tokens, cache_read, cache_write, reasoning,
               cost_usd, ttft_ms, latency_ms, status, raw_json
        FROM usage_records WHERE {cond}
        ORDER BY ts DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, page_size, offset],
    ).fetchall()

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
        gen_sec = ((item["latency_ms"] or 0) - (item["ttft_ms"] or 0)) / 1000
        if item["ttft_ms"] and gen_sec > 0:
            item["gen_tok_per_sec"] = round(out_tok / gen_sec, 2)
        else:
            item["gen_tok_per_sec"] = item["tok_per_sec"]

        # 分项拆解（按当前价格表）
        br = None
        if prices is not None:
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


@router.get("/usage")
def usage(
    request: Request,
    range_: str = Query("today", alias="range"),
    source: str | None = None,
    client_key: str | None = None,
    group_by: str = Query("model"),
):
    start, end = _bounds(range_)
    conn = _store(request).read_conn()

    col = {"model": "model", "source": "source", "account": "account",
           "client_key": "client_key"}.get(group_by, "model")

    where = ["ts BETWEEN ? AND ?"]
    params: list[Any] = [start, end]
    if source:
        where.append("source = ?")
        params.append(source)
    if client_key:
        where.append("client_key = ?")
        params.append(client_key)

    sql = f"""
        SELECT COALESCE({col},'(unknown)') AS grp,
               COALESCE(SUM(input_tokens),0)  AS input,
               COALESCE(SUM(output_tokens),0) AS output,
               COALESCE(SUM(cache_read),0)    AS cache_read,
               COALESCE(SUM(reasoning),0)     AS reasoning,
               COUNT(*)                       AS requests,
               COALESCE(SUM(cost_usd),0)      AS cost
        FROM usage_records
        WHERE {' AND '.join(where)}
        GROUP BY grp
        ORDER BY input + output DESC
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
    conn = _store(request).read_conn()

    if bucket == "hour":
        fmt = "%Y-%m-%d %H:00"
        if range_ in ("today", "1d"):
            now = int(time.time())
            start, end = now - 86400, now
        else:
            start, end = _bounds(range_)
    else:
        fmt = "%Y-%m-%d"
        start, end = _bounds(range_)

    where = ["ts BETWEEN ? AND ?"]
    params: list[Any] = [start, end]
    if model:
        # 支持 "opgo/kimi-k3" 或纯 "kimi-k3"
        bare = model.split("/", 1)[1] if "/" in model else model
        where.append("(model = ? OR requested_model = ? OR model = ?)")
        params.extend([bare, model, model])
    if source:
        where.append("source = ?")
        params.append(source)

    rows = conn.execute(
        f"""
        SELECT strftime('{fmt}', ts, 'unixepoch', 'localtime') AS bucket,
               source,
               COALESCE(SUM(input_tokens),0)  AS input,
               COALESCE(SUM(output_tokens),0) AS output,
               COALESCE(SUM(cache_read),0)    AS cache_read,
               COALESCE(SUM(reasoning),0)     AS reasoning,
               COUNT(*)                       AS requests,
               COALESCE(SUM(cost_usd),0)      AS cost
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
            b, {"bucket": b, "input": 0, "output": 0, "cache_read": 0,
                "reasoning": 0, "requests": 0, "cost": 0.0, "by_source": {}}
        )
        for k in ("input", "output", "cache_read", "reasoning", "requests"):
            item[k] += r[k]
        item["cost"] += r["cost"] or 0
        item["by_source"][r["source"]] = r["input"] + r["output"] + r["cache_read"]

    result = []
    for v in merged.values():
        v["cost"] = round(v["cost"], 6)
        result.append(v)
    return result


@router.get("/models/active")
def active_models(request: Request, range_: str = Query("today", alias="range")):
    """当前时间范围内有请求的模型列表，用于图表线条选择。"""
    start, end = _bounds(range_)
    conn = _store(request).read_conn()
    rows = conn.execute(
        """
        SELECT source, model, COUNT(*) AS requests,
               COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens
        FROM usage_records
        WHERE ts BETWEEN ? AND ?
        GROUP BY source, model
        ORDER BY tokens DESC
        """,
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/quota")
def quota(request: Request):
    conn = _store(request).read_conn()
    rows = conn.execute(
        """
        SELECT q.source, q.account, q.window, q.remaining_pct,
               q.remaining_amt, q.currency, q.resets_at, q.ts
        FROM quota_snapshots q
        JOIN (
            SELECT source, COALESCE(account,'') AS acct,
                   COALESCE(window,'') AS win, MAX(ts) AS max_ts
            FROM quota_snapshots
            GROUP BY source, acct, win
        ) latest
          ON q.source = latest.source
         AND COALESCE(q.account,'') = latest.acct
         AND COALESCE(q.window,'')  = latest.win
         AND q.ts = latest.max_ts
        ORDER BY q.source, q.account, q.window
        """
    ).fetchall()

    alerter = getattr(request.app.state, "alerter", None)
    warn_pct = alerter.cfg.warn_pct if alerter else 20.0
    critical_pct = alerter.cfg.critical_pct if alerter else 10.0

    out = []
    for r in rows:
        item = dict(r)
        pct = item.get("remaining_pct")
        if pct is None:
            item["level"] = "unknown"
        elif pct <= critical_pct:
            item["level"] = "critical"
        elif pct <= warn_pct:
            item["level"] = "warn"
        else:
            item["level"] = "ok"
        out.append(item)
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
    conn = _store(request).read_conn()

    rows = conn.execute(
        """
        SELECT source, model,
               COUNT(*) AS requests,
               COALESCE(SUM(input_tokens),0) AS input,
               COALESCE(SUM(output_tokens),0) AS output,
               COALESCE(SUM(cache_read),0) AS cache_read,
               COALESCE(SUM(cost_usd),0) AS cost,
               SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS priced
        FROM usage_records
        WHERE ts BETWEEN ? AND ?
        GROUP BY source, model
        ORDER BY cost DESC, input + output DESC
        """,
        (start, end),
    ).fetchall()

    out = []
    billing = _billing_map(request)
    for r in rows:
        item = dict(r)
        item["cost"] = round(item["cost"] or 0, 6)
        item["avg_cost_per_request"] = (
            round(item["cost"] / item["requests"], 8) if item["requests"] else 0
        )
        info = billing.get(item["source"], {"mode": "payg", "alias": item["source"]})
        item["billing_mode"] = info["mode"]
        item["alias"] = info["alias"]
        out.append(item)
    return out
