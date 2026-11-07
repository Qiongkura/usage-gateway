"""探测各上游模型的可用性。

对每个模型发一个最小请求，记录结果：
  ok        - 正常返回
  gone      - HTTP 410 Gone
  not_found - HTTP 404
  timeout   - 超时
  error     - 其他错误

结果写入 model_health.json，网关启动时读取并过滤掉不可用的模型。

用法：
    python -m app.probe_models            # 探测所有上游
    python -m app.probe_models axon       # 只探测 axon
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import httpx

from .config import load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("probe")

ROOT = Path(__file__).resolve().parent.parent
HEALTH_FILE = ROOT / "model_health.json"

# 探测用的最小请求
# 注意：max_tokens 不能设成 1。部分上游（例如 antigravity 的 Gemini）在
# max_tokens=1 时会直接中断流，探测会误判成 server_error，把可用模型隐藏掉。
PROBE_BODY = {
    "messages": [{"role": "user", "content": "hi"}],
    "max_tokens": 16,
    "stream": False,
}

# 并发数
CONCURRENCY = 6
# 单模型超时（秒）
TIMEOUT = 45


async def probe_one(
    client: httpx.AsyncClient,
    upstream,
    model: str,
    sem: asyncio.Semaphore,
) -> tuple[str, dict]:
    """探测单个模型。"""
    async with sem:
        url = f"{upstream.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if upstream.api_key:
            headers["authorization"] = f"Bearer {upstream.api_key}"
        if "opencode.ai" in upstream.base_url:
            headers["x-opencode-session"] = f"probe-{model}"
            headers["user-agent"] = "usage-gateway-probe/0.1.0"

        body = dict(PROBE_BODY, model=model)
        started = time.perf_counter()
        try:
            resp = await client.post(url, headers=headers, json=body)
            elapsed = round((time.perf_counter() - started) * 1000)

            if resp.status_code == 200:
                return model, {"status": "ok", "ms": elapsed}

            # 提取错误信息
            detail = ""
            try:
                payload = resp.json()
                err = payload.get("error") or {}
                detail = str(err.get("message") or err.get("type") or "")[:120]
            except Exception:
                detail = resp.text[:120]

            if resp.status_code == 410:
                status = "gone"
            elif resp.status_code == 404:
                status = "not_found"
            elif resp.status_code == 429:
                status = "rate_limited"
            elif resp.status_code >= 500:
                status = "server_error"
            else:
                status = "error"

            return model, {"status": status, "code": resp.status_code, "detail": detail, "ms": elapsed}

        except httpx.TimeoutException:
            return model, {"status": "timeout", "ms": TIMEOUT * 1000}
        except Exception as exc:
            return model, {"status": "error", "detail": str(exc)[:120], "ms": 0}


async def probe_upstream(upstream, models: list[str]) -> dict[str, dict]:
    """探测一个上游的所有模型。"""
    proxy = None
    if not upstream.no_proxy:
        import os
        proxy = (
            os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
        )

    kwargs: dict = {
        "timeout": httpx.Timeout(TIMEOUT, connect=20.0),
        "trust_env": False,
    }
    if proxy:
        kwargs["proxy"] = proxy

    sem = asyncio.Semaphore(CONCURRENCY)
    results: dict[str, dict] = {}

    async with httpx.AsyncClient(**kwargs) as client:
        tasks = [probe_one(client, upstream, m, sem) for m in models]
        done = 0
        for coro in asyncio.as_completed(tasks):
            model, result = await coro
            results[model] = result
            done += 1
            mark = "OK " if result["status"] == "ok" else "XX "
            log.info("[%s] %s %-32s %s", upstream.name, mark, model, result["status"])

    return results


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    cfg = load_config()

    # 先拉取模型列表
    import asyncio as _aio

    async def discover():
        out = {}
        for up in cfg.upstreams:
            if only and up.name != only and up.prefix != only:
                continue
            if up.models:
                out[up.name] = up.models
                continue
            headers = {}
            if up.api_key:
                headers["authorization"] = f"Bearer {up.api_key}"
            proxy = cfg.proxy_for(up)
            kwargs: dict = {"timeout": 20, "trust_env": False}
            if proxy:
                kwargs["proxy"] = proxy
            try:
                async with httpx.AsyncClient(**kwargs) as c:
                    r = await c.get(f"{up.base_url}/models", headers=headers)
                    r.raise_for_status()
                    payload = r.json()
                    items = payload.get("data") if isinstance(payload, dict) else payload
                    out[up.name] = [m["id"] for m in (items or []) if isinstance(m, dict) and m.get("id")]
                    log.info("[%s] %d models", up.name, len(out[up.name]))
            except Exception as exc:
                log.warning("[%s] discovery failed: %s", up.name, exc)
        return out

    model_map = _aio.run(discover())

    # 读取已有的健康记录
    existing: dict = {}
    if HEALTH_FILE.exists():
        try:
            existing = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        except Exception:
            existing = {}

    # 逐个上游探测
    for up in cfg.upstreams:
        models = model_map.get(up.name)
        if not models:
            continue
        log.info("=" * 60)
        log.info("probing %s (%d models)...", up.name, len(models))
        results = asyncio.run(probe_upstream(up, models))

        existing.setdefault(up.name, {})
        existing[up.name].update(results)

        ok = sum(1 for r in results.values() if r["status"] == "ok")
        log.info("[%s] %d/%d available", up.name, ok, len(results))

    HEALTH_FILE.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("saved to %s", HEALTH_FILE)

    # 汇总
    print()
    print("=" * 60)
    for up_name, models in existing.items():
        ok = [m for m, r in models.items() if r["status"] == "ok"]
        bad = [m for m, r in models.items() if r["status"] != "ok"]
        print(f"{up_name}: {len(ok)} ok, {len(bad)} unavailable")
        if bad:
            for m in bad[:10]:
                print(f"    - {m}: {models[m]['status']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
