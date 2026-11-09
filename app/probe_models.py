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
import math
import os
from contextlib import contextmanager, ExitStack
import sys
import tempfile
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from .config import MODEL_HEALTH_TTL, load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("probe")

ROOT = Path(__file__).resolve().parent.parent
HEALTH_FILE = ROOT / "model_health.json"
LOCK_FILE = ROOT / "model_health.lock"
COOLDOWN_FILE = ROOT / "model_probe_cooldown.json"


@contextmanager
def _file_lock(path):
    """Non-blocking OS lock; the lock file stays in place to avoid inode races."""
    with open(path, "a+b") as f:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
        else:
            import fcntl
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
        try:
            yield True
        finally:
            if os.name == "nt":
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextmanager
def probe_lock(cfg):
    """Hold both the legacy project lock and the shared data-directory lock."""
    with ExitStack() as stack:
        try:
            data_dir = Path(cfg.data_dir)
            data_dir.mkdir(parents=True, exist_ok=True)
            acquired = stack.enter_context(_file_lock(LOCK_FILE))
            if acquired:
                acquired = stack.enter_context(_file_lock(data_dir / "model_probe.lock"))
        except OSError:
            log.exception("cannot acquire probe lock")
            acquired = False
        yield acquired

# 探测用的最小请求
# 注意：max_tokens 不能设成 1。部分上游（例如 antigravity 的 Gemini）在
# max_tokens=1 时会直接中断流，探测会误判成 server_error，把可用模型隐藏掉。
PROBE_BODY = {
    "messages": [{"role": "user", "content": "hi"}],
    "max_tokens": 16,
    "stream": False,
}

# 并发数
CONCURRENCY = 1
PROBE_INTERVAL = 1.0
MAX_RETRIES = 2
MAX_BACKOFF = 30.0
CONFIRMATIONS = 2
HEALTH_TTL = MODEL_HEALTH_TTL
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

            # 上游返回内容可能包含密钥或请求数据，不持久化或记录。
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

            result = {"status": status, "code": resp.status_code, "ms": elapsed}
            if status == "rate_limited":
                result["retry_after"] = retry_after_seconds(resp.headers.get("Retry-After"))
            return model, result

        except httpx.TimeoutException:
            return model, {"status": "timeout", "ms": TIMEOUT * 1000}
        except httpx.HTTPError:
            return model, {"status": "error", "ms": 0}


def retry_after_seconds(value: str | None) -> float:
    try:
        delay = float(value or "")
    except ValueError:
        try:
            date = parsedate_to_datetime(value or "")
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = (date - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 0.0
    return max(0.0, delay) if math.isfinite(delay) else 0.0


def merge_result(previous: dict, result: dict, now: float | None = None) -> dict:
    """Only distinct probe runs confirm a missing model; transient errors preserve state."""
    now = time.time() if now is None else now
    previous = previous if isinstance(previous, dict) else {}
    status = result.get("status")
    if status in {"gone", "not_found"}:
        checked = previous.get("checked_at")
        count = previous.get("missing_count")
        valid = (previous.get("status") in {"gone", "not_found"}
                 and isinstance(checked, (int, float)) and 0 <= now - checked <= HEALTH_TTL
                 and isinstance(count, int) and count >= 0)
        return {**result, "missing_count": count + 1 if valid else 1, "checked_at": now}
    if status != "ok" and previous.get("status") in {"gone", "not_found"}:
        # 已确认状态保留到过期；未确认的连续缺失被瞬时故障打断。
        if isinstance(previous.get("missing_count"), int) and previous["missing_count"] >= CONFIRMATIONS:
            return previous
    return {**result, "checked_at": now}


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
        for index, model in enumerate(dict.fromkeys(models)):
            if index:
                await asyncio.sleep(PROBE_INTERVAL)
            for attempt in range(MAX_RETRIES + 1):
                _, result = await probe_one(client, upstream, model, sem)
                if result["status"] != "rate_limited":
                    break
                delay = max(2 ** attempt, result.get("retry_after", 0))
                if delay > MAX_BACKOFF or attempt == MAX_RETRIES:
                    result["retry_after"] = delay
                    break
                await asyncio.sleep(delay)
            results[model] = result
            if result["status"] == "rate_limited":
                # 限速通常针对上游账号，剩余模型也不能提前请求。
                break
            mark = "OK " if result["status"] == "ok" else "XX "
            log.info("[%s] %s %-32s %s", upstream.name, mark, model, result["status"])

    return results


def atomic_write_json(path: Path, data: dict) -> None:
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
            temp_name = f.name
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


class CooldownWriteError(RuntimeError):
    pass


def cooldown_path(cfg) -> Path:
    return Path(cfg.data_dir) / "model_probe_cooldown.json"


def load_cooldowns(cfg) -> dict:
    """Merge legacy and persistent state conservatively; invalid state fails closed."""
    merged = {}
    legacy_present = False
    persistent = {}
    for path in dict.fromkeys((COOLDOWN_FILE, cooldown_path(cfg))):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        if path == COOLDOWN_FILE:
            legacy_present = True
        if path == cooldown_path(cfg):
            persistent = state
        if not isinstance(state, dict):
            raise ValueError("invalid probe cooldown state")
        for name, until in state.items():
            if not isinstance(until, (int, float)) or isinstance(until, bool) or not math.isfinite(until):
                raise ValueError("invalid probe cooldown")
            merged[name] = max(merged.get(name, 0), until)
    # Copy legacy limits before allowing discovery; leave the old file untouched.
    if legacy_present and merged != persistent:
        save_cooldowns(cfg, merged)
    return merged


def save_cooldowns(cfg, cooldowns: dict) -> None:
    path = cooldown_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, cooldowns)


def main(only: str | None = None, *, locked: bool = False, cfg=None) -> int:
    cfg = load_config() if cfg is None else cfg
    if not locked:
        with probe_lock(cfg) as acquired:
            if not acquired:
                log.info("probe already running")
                return 1
            return main(only, locked=True, cfg=cfg)
    failed = False
    try:
        cooldowns = load_cooldowns(cfg)
    # A corrupt cooldown file must fail closed, not bypass a provider's limit.
    except (OSError, ValueError):
        log.error("cannot read probe cooldown state")
        return 1

    # 先拉取模型列表
    import asyncio as _aio

    async def discover():
        out = {}
        nonlocal failed
        for up in cfg.upstreams:
            if only and up.name != only and up.prefix != only:
                continue
            until = cooldowns.get(up.name, 0)
            if not isinstance(until, (int, float)) or not math.isfinite(until):
                raise ValueError("invalid probe cooldown")
            if until > time.time():
                failed = True
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
                    if r.status_code == 429:
                        delay = max(1, retry_after_seconds(r.headers.get("Retry-After")))
                        cooldowns[up.name] = time.time() + delay
                        # Persist immediately so later runs cannot bypass the limit.
                        try:
                            save_cooldowns(cfg, cooldowns)
                        except OSError as exc:
                            raise CooldownWriteError("cannot persist probe cooldown") from exc
                        failed = True
                        log.warning("[%s] discovery rate limited", up.name)
                        continue
                    r.raise_for_status()
                    payload = r.json()
                    items = payload.get("data") if isinstance(payload, dict) else payload
                    out[up.name] = [m["id"] for m in (items or []) if isinstance(m, dict) and m.get("id")]
                    log.info("[%s] %d models", up.name, len(out[up.name]))
            except CooldownWriteError:
                raise
            except Exception:
                failed = True
                log.warning("[%s] discovery failed", up.name)
        return out

    model_map = _aio.run(discover())

    # 探测进程拿到的最新列表同步到当前运行配置；否则启动发现失败后，
    # 即使探针成功，/v1/models 仍会一直为空直到下次重启。
    for up in cfg.upstreams:
        discovered = model_map.get(up.name)
        if discovered and not up.models:
            up.discovered_models = list(discovered)

    # 读取已有的健康记录
    existing: dict = {}
    if HEALTH_FILE.exists():
        try:
            existing = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, ValueError):
            existing = {}
    existing = {name: records for name, records in existing.items() if isinstance(records, dict)}

    # 逐个上游探测
    for up in cfg.upstreams:
        models = model_map.get(up.name)
        if not models:
            continue
        log.info("=" * 60)
        log.info("probing %s (%d models)...", up.name, len(models))
        results = asyncio.run(probe_upstream(up, models))

        existing.setdefault(up.name, {})
        for model, result in results.items():
            if result["status"] == "rate_limited":
                cooldowns[up.name] = time.time() + max(1, result.get("retry_after", 0))
                # Persist before writing health, while still holding the cross-process lock.
                save_cooldowns(cfg, cooldowns)
            if result["status"] in {"rate_limited", "timeout", "server_error", "error"}:
                failed = True
            existing[up.name][model] = merge_result(existing[up.name].get(model, {}), result)

        ok = sum(1 for r in results.values() if r["status"] == "ok")
        log.info("[%s] %d/%d available", up.name, ok, len(results))

    # 同目录临时文件及替换，避免中途崩溃留下半份 JSON。
    atomic_write_json(HEALTH_FILE, existing)
    log.info("saved to %s", HEALTH_FILE)

    # 汇总
    print()
    print("=" * 60)
    for up_name, models in existing.items():
        ok = [m for m, r in models.items() if isinstance(r, dict) and r.get("status") == "ok"]
        bad = [m for m, r in models.items() if isinstance(r, dict) and r.get("status") != "ok"]
        print(f"{up_name}: {len(ok)} ok, {len(bad)} unavailable")
        if bad:
            for m in bad[:10]:
                print(f"    - {m}: {models[m].get('status', 'unknown')}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
