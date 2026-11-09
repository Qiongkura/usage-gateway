"""Usage Gateway 入口。

职责：
  1. 对客户端暴露 OpenAI / Anthropic 兼容接口，用自己发的 key 鉴权
  2. 按模型名路由到上游（Antigravity Tools / Mistral / 中转站）
  3. 记录每条请求的真实 token
  4. 提供统计 API 和 Web 看板
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .alerts import QuotaAlerter
from .api import stats as stats_api
from .collectors.antigravity import AntigravityCollector
from .collectors.base import Collector
from .collectors.mistral import MistralBillingCollector
from .collectors.opencode import OpenCodeCollector
from .collectors.relay import RelayCollector
from .collectors.vibe import VibeCollector
from .collectors.zen import ZenUsageCollector
from .config import (
    MODEL_HEALTH_TTL,
    apply_model_health,
    is_loopback_host,
    load_config,
    load_model_health,
)
from .db import Store, session_context
from .models import UsageRecord
from .tracing import Trace
from .session_attribution import SessionAttributor, validate_session_id
from .health import HealthTracker
from .pricing import load_prices
from .proxy import close_http, extract_key, forward_with_failover, unauthorized
from .scheduler import Scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("usage-gateway")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# 由 main() 在启动时注入，供 /api/admin/shutdown 优雅退出使用
_UVICORN_SERVER = None


def _safe_exception_context(exc: Exception) -> str:
    """Return useful location metadata without leaking exception messages."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return type(exc).__name__
    frame = frames[-1]
    return f"{type(exc).__name__} at {Path(frame.filename).name}:{frame.name}:{frame.lineno}"


def build_collectors(cfg) -> list[Collector]:
    collectors: list[Collector] = []

    for up in cfg.upstreams:
        src = up.usage_source or {}
        if src.get("type") == "antigravity_sqlite":
            collectors.append(AntigravityCollector(src.get("data_dir") or None))

        bal = up.balance or {}
        if bal.get("type") == "mistral_billing":
            collectors.append(
                MistralBillingCollector(
                    name=up.name,
                    base_url=bal.get("base_url") or up.base_url,
                    api_key=up.api_key,
                    proxy=cfg.proxy_for(up),
                )
            )
        elif bal.get("type") == "zen_usage":
            collectors.append(
                ZenUsageCollector(
                    name=up.name,
                    base_url=bal.get("base_url") or "https://opencode.ai",
                    api_key=up.api_key,
                    proxy=cfg.proxy_for(up),
                )
            )
        elif bal.get("base_url") and bal.get("access_token"):
            collectors.append(
                RelayCollector(
                    name=up.name,
                    base_url=bal["base_url"],
                    access_token=bal["access_token"],
                    user_id=str(bal.get("user_id") or ""),
                )
            )

    local = cfg.local_sources or {}
    oc = local.get("opencode") or {}
    if oc.get("enabled", True):
        collectors.append(OpenCodeCollector(oc.get("data_dir") or None))
    vb = local.get("vibe") or {}
    if vb.get("enabled", True):
        collectors.append(VibeCollector(
            vb.get("home") or None,
            state_file=cfg.data_dir / "vibe_state.json",
        ))

    return collectors


async def discover_models(cfg) -> None:
    """启动时为没有显式配置 models 的上游拉取模型列表。"""
    from .probe_models import probe_lock, load_cooldowns

    with probe_lock(cfg) as acquired:
        if not acquired:
            log.warning("probe lock busy; skipping startup model discovery")
            return
        try:
            cooldowns = load_cooldowns(cfg)
        except (OSError, ValueError):
            log.error("cannot read probe cooldown state; skipping startup model discovery")
            return
        await _discover_models_locked(cfg, cooldowns)


async def _discover_models_locked(cfg, cooldowns) -> None:
    import httpx
    from .probe_models import CooldownWriteError, retry_after_seconds, save_cooldowns

    for up in cfg.upstreams:
        if up.models:
            continue
        if cooldowns.get(up.name, 0) > time.time():
            log.info("[%s] skipping model discovery during probe cooldown", up.name)
            continue
        headers = {}
        if up.api_key:
            headers["authorization"] = f"Bearer {up.api_key}"
        proxy = cfg.proxy_for(up)
        kwargs: dict = {"timeout": 20, "trust_env": False}
        if proxy:
            kwargs["proxy"] = proxy
        # 启动时网络/代理可能尚未就绪；重试一次，避免空模型列表锁死整个进程。
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(**kwargs) as client:
                    resp = await client.get(f"{up.base_url}/models", headers=headers)
                    if resp.status_code == 429:
                        cooldowns[up.name] = time.time() + max(1, retry_after_seconds(resp.headers.get("Retry-After")))
                        try:
                            save_cooldowns(cfg, cooldowns)
                        except OSError as exc:
                            raise CooldownWriteError("cannot persist startup discovery cooldown") from exc
                        log.warning("[%s] model discovery rate limited", up.name)
                        break
                    resp.raise_for_status()
                    payload = resp.json()
                    items = payload.get("data") if isinstance(payload, dict) else payload
                    up.discovered_models = [
                        str(m.get("id")) for m in (items or []) if isinstance(m, dict) and m.get("id")
                    ]
                    log.info("[%s] discovered %d models", up.name, len(up.discovered_models))
                    break
            except CooldownWriteError:
                raise
            except Exception as exc:
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                log.warning("[%s] model discovery failed: %s", up.name, exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    await discover_models(cfg)

    root = Path(__file__).resolve().parent.parent

    # 应用模型可用性探测结果
    health = load_model_health(root / "model_health.json")
    if health:
        apply_model_health(cfg.upstreams, health)
        for up in cfg.upstreams:
            total = len(up.models or up.discovered_models)
            avail = len(up.all_models)
            if up.unavailable_models:
                log.info(
                    "[%s] %d/%d models available (%d hidden)",
                    up.name, avail, total, total - avail,
                )

    cfg.prices = load_prices(
        root / "pricing.yaml",
        auto_dirs={"opencode": root / "zen_prices.json"},
    )

    store = Store(cfg.data_dir / "usage.db")
    collectors = build_collectors(cfg)
    alerter = QuotaAlerter(cfg.alerts) if cfg.alerts.enabled else None

    app.state.cfg = cfg
    app.state.store = store
    app.state.collectors = collectors
    app.state.alerter = alerter
    app.state.tracker = HealthTracker(cfg.upstreams)
    app.state.server = _UVICORN_SERVER

    sched = Scheduler(
        store, collectors, cfg.collect_interval_sec,
        alerter=alerter, retention_days=cfg.retention_days, prices=cfg.prices,
        cfg=cfg,
    )
    sched.start()
    app.state.sched = sched

    if alerter is not None:
        log.info(
            "quota alerts: warn<=%.0f%% critical<=%.0f%% cooldown=%ds channels=%s",
            cfg.alerts.warn_pct, cfg.alerts.critical_pct,
            cfg.alerts.cooldown_sec, ",".join(alerter.notifier.active_channels()),
        )

    log.info("gateway listening on http://%s:%s", cfg.host, cfg.port)
    log.info("client keys: %s", ", ".join(k.name for k in cfg.client_keys) or "(none)")
    log.info("upstreams: %s", ", ".join(u.name for u in cfg.upstreams) or "(none)")
    log.info("collectors: %s", ", ".join(c.name for c in collectors) or "(none)")
    if not cfg.client_keys:
        log.warning("没有配置任何客户端 key，所有请求都会被拒绝")
    if not is_loopback_host(cfg.host):
        disabled = []
        if not cfg.stats_key:
            disabled.append("statistics API")
        if not cfg.admin_key:
            disabled.append("management API")
        if disabled:
            log.warning(
                "non-loopback bind without required keys; disabled until configured: %s",
                ", ".join(disabled),
            )

    try:
        yield
    finally:
        sched.shutdown()
        await close_http()
        store.close()


app = FastAPI(title="Usage Gateway", version="0.1.0", lifespan=lifespan)
app.include_router(stats_api.router)


# --------------------------------------------------------------------------
# 代理端点
# --------------------------------------------------------------------------

def _auth(request: Request):
    cfg = request.app.state.cfg
    raw = extract_key(request)
    if not raw:
        return None, unauthorized("Missing API key")
    ck = cfg.find_key(raw)
    if ck is None:
        return None, unauthorized("Invalid API key")
    return ck, None


async def _reject(request: Request, response: JSONResponse, kind: str, client_key: str | None = None):
    request_id = uuid.uuid4().hex
    response.headers["x-trace-id"] = request_id
    try:
        ts = int(time.time())
        trace = Trace(request_id)
        trace.add("reject", 0, kind=kind, status=response.status_code)
        record = UsageRecord(ts=ts, source="gateway", client_key=client_key,
                             status=response.status_code, raw={"request_id": request_id})
        request.state.recording_attempted = True
        await asyncio.to_thread(request.app.state.store.record_request,
            usage_record=record, trace=trace.finish(), ts=ts, source="gateway",
            client_key=client_key, status=response.status_code, result="rejected",
            error_stage="entry", error_kind=kind, error_status=response.status_code,
            session_key=getattr(request.state, "session_key", None),
            session_source=getattr(request.state, "session_source", None))
    except Exception:
        # A rejected request must not become a 500 if bookkeeping fails.
        log.error("Failed to record rejected request (trace_id=%s)", request_id)
    return response


async def _handle(request: Request, path: str):
    try:
        return await _handle_entry(request, path)
    except Exception as exc:
        # Forwarding and rejection may already have written a record, even if
        # they subsequently fail. Never invent a second billable request.
        if getattr(request.state, "recording_attempted", False):
            request_id = uuid.uuid4().hex
            log.error(
                "Request failed after accounting was delegated; no fallback record written "
                "(error_id=%s, context=%s)",
                request_id,
                _safe_exception_context(exc),
            )
            return JSONResponse({"error": {"message": "Internal server error", "type": "internal_error"}},
                                status_code=500, headers={"x-trace-id": request_id})
        request_id = uuid.uuid4().hex
        ts = int(time.time())
        log.error(
            "Unexpected entry failure (trace_id=%s, context=%s)",
            request_id,
            _safe_exception_context(exc),
        )
        try:
            trace = Trace(request_id)
            trace.add("entry", 0, kind="unexpected_error", status=500)
            record = UsageRecord(ts=ts, source="gateway", status=500,
                                 raw={"request_id": request_id})
            await asyncio.to_thread(
                request.app.state.store.record_request,
                usage_record=record, trace=trace.finish(), ts=ts, source="gateway",
                status=500, result="internal_error", error_stage="entry",
                error_kind="unexpected_error", error_status=500,
                session_key=getattr(request.state, "session_key", None),
                session_source=getattr(request.state, "session_source", None),
            )
        except Exception:
            log.error("Failed to record unexpected entry failure (trace_id=%s)", request_id)
        return JSONResponse({"error": {"message": "Internal server error", "type": "internal_error"}},
                            status_code=500, headers={"x-trace-id": request_id})


async def _handle_entry(request: Request, path: str):
    ck, err = _auth(request)
    if err:
        return await _reject(request, err, "authentication")

    session_values = request.headers.getlist("x-session-id")
    try:
        if len(session_values) > 1:
            raise ValueError("duplicate session header")
        if session_values:
            validate_session_id(session_values[0])
            attributor = getattr(request.app.state, "session_attributor", None)
            if attributor is None:
                attributor = SessionAttributor(request.app.state.store.path.parent)
                request.app.state.session_attributor = attributor
            request.state.session_key = attributor.fingerprint(session_values[0], client_id=ck.name, source="header")
            request.state.session_source = "header"
    except ValueError:
        return await _reject(request, JSONResponse({"error": {"message": "Invalid session header", "type": "invalid_request_error"}}, status_code=400), "invalid_session", ck.name)

    cfg = request.app.state.cfg
    store = request.app.state.store

    body = await request.body()
    raw_model = None
    payload = None
    if body:
        try:
            import json as _json
            payload = _json.loads(body)
            if isinstance(payload, dict):
                raw_model = payload.get("model")
        except (ValueError, TypeError):
            payload = None

    if raw_model is None or raw_model == "":
        return await _reject(request, JSONResponse(
            {"error": {"message": "Missing 'model' in request body", "type": "invalid_request_error"}},
            status_code=400,
        ), "missing_model", ck.name)
    if not isinstance(raw_model, str) or not raw_model.strip():
        return await _reject(request, JSONResponse(
            {"error": {"message": "'model' must be a non-empty string", "type": "invalid_request_error"}},
            status_code=400,
        ), "invalid_model", ck.name)

    # 解析前缀：axon/kimi-k3 -> (relay-axon, kimi-k3)
    upstream, model, route_error = cfg.route(raw_model)

    if upstream is None:
        return await _reject(request, JSONResponse(
            {"error": {"message": route_error, "type": "invalid_request_error"}},
            status_code=400 if "must be prefixed" in route_error else 404,
        ), "route", ck.name)

    # 模型白名单检查（支持前缀写法）
    if ck.allow_models:
        allowed = set(ck.allow_models)
        if raw_model not in allowed and model not in allowed:
            return await _reject(request, JSONResponse(
                {"error": {"message": f"Model '{raw_model}' not allowed for key '{ck.name}'",
                           "type": "invalid_request_error"}},
                status_code=403,
            ), "model_not_allowed", ck.name)

    # 构造候选上游列表。
    #
    # 默认 failover=False：候选只有用户明确指定的那一个上游。
    # 失败立即返回错误，绝不会静默切到另一个（可能贵得多的）上游。
    #
    # 只有显式配置 failover: true 时才会：
    #   - 使用同别名的多个上游组成分组（负载均衡）
    #   - 失败后按 retry_rounds 重试
    tracker = request.app.state.tracker
    prefix, sep, _ = raw_model.partition("/")
    explicit_up = cfg.find_by_prefix(prefix) if sep else None

    if cfg.failover and explicit_up is not None:
        def confirmed_missing(up):
            checked = up._model_health_checked
            if checked is None:
                return model in up.unavailable_models
            timestamp = checked.get(model)
            return timestamp is not None and 0 <= time.time() - timestamp <= MODEL_HEALTH_TTL

        candidates = [
            up for up in cfg.upstreams
            if up.prefix == explicit_up.prefix
            and (up is explicit_up or not confirmed_missing(up))
        ]
        candidates.sort(key=lambda u: u.priority)
        if not candidates:
            candidates = [explicit_up]
    else:
        # 严格模式：锁死指定的上游
        candidates = [upstream]

    max_rounds = max(1, int(cfg.retry_rounds or 1)) if cfg.failover else 1

    # model 改写与兼容层统一在 forward_with_failover 内一次序列化完成，
    # 避免对几百 KB 的多轮上下文重复 json.loads / json.dumps。
    # From this point the proxy owns accounting and may persist before raising.
    request.state.recording_attempted = True
    token = session_context.set((getattr(request.state, "session_key", None),
                                 getattr(request.state, "session_source", None)))
    try:
        response = await forward_with_failover(
            request=request, cfg=cfg, store=store, tracker=tracker,
            path=path, body=body, client_key_name=ck.name, model=model,
            requested_model=raw_model, candidates=candidates,
            allow_retry=cfg.failover,
            max_rounds=max_rounds,
            parsed_payload=payload if isinstance(payload, dict) else None,
        )
        # StreamingResponse executes after this context is reset; carry the
        # fingerprint explicitly into the stream generator instead.
        if hasattr(response, "body_iterator"):
            iterator = response.body_iterator
            session = session_context.get()
            async def attributed_stream():
                stream_token = session_context.set(session)
                try:
                    async for chunk in iterator:
                        yield chunk
                finally:
                    try:
                        close = getattr(iterator, "aclose", None)
                        if close is not None:
                            await close()
                    finally:
                        session_context.reset(stream_token)
            response.body_iterator = attributed_stream()
        return response
    finally:
        session_context.reset(token)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    return await _handle(request, "/chat/completions")


@app.post("/v1/completions")
async def completions(request: Request):
    return await _handle(request, "/completions")


@app.post("/v1/messages")
async def messages(request: Request):
    return await _handle(request, "/messages")


@app.post("/v1/responses")
async def responses(request: Request):
    return await _handle(request, "/responses")


@app.get("/v1/models")
async def list_models(request: Request):
    ck, err = _auth(request)
    if err:
        return err
    return stats_api.models(request)


# --------------------------------------------------------------------------
# Web 看板
# --------------------------------------------------------------------------

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    def _no_cache(path: Path):
        """页面文件禁缓存：改版后 WebView2/浏览器不会拿到旧页面。"""
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    @app.get("/")
    async def index():
        return _no_cache(WEB_DIR / "index.html")

    @app.get("/float")
    async def float_window():
        """实时 tok/s 悬浮窗（无边框置顶原生窗）。"""
        return _no_cache(WEB_DIR / "float.html")
else:
    @app.get("/")
    async def index_fallback():
        return JSONResponse({
            "service": "usage-gateway",
            "hint": "web/index.html 不存在，请访问 /api/summary 或 /docs",
        })


def main() -> None:
    import uvicorn

    global _UVICORN_SERVER

    cfg = load_config()

    # 直接传 app 对象，不要用 "app.main:app" 字符串：
    # 用 python -m app.main 启动时，本模块身份是 __main__，
    # 字符串形式会让 uvicorn 再导入一份 app.main，全局变量就不共享了。
    config = uvicorn.Config(
        app,
        host=cfg.host,
        port=cfg.port,
        log_level="info",
        reload=False,
    )
    server = uvicorn.Server(config)
    _UVICORN_SERVER = server
    server.run()


if __name__ == "__main__":
    main()
