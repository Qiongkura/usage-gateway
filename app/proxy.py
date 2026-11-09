"""网关代理：客户端 -> 本网关 -> 上游。

客户端只持有 gateway.client_keys 里的 key；上游 key 仅存在于服务端配置。
所有经过的请求都会被计量并落库。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import Config, Upstream
from .db import Store
from .models import UsageRecord
from .pricing import PriceTable
from .tracing import Trace
from .sse import SSEParser, SSELimitError
from .sse_heartbeat import with_sse_heartbeat

log = logging.getLogger("usage-gateway.proxy")

# 复用连接池：按代理分组
_HTTP_POOL: dict[str, httpx.AsyncClient] = {}


_PROXY_CACHE: dict[str, tuple[float, bool]] = {}


def _parse_proxy_host_port(proxy: str) -> tuple[str, int]:
    from urllib.parse import urlparse

    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    return parsed.hostname or "127.0.0.1", parsed.port or 10809


def _proxy_alive(proxy: str, ttl: float = 30.0) -> bool:
    """检查代理端口是否可连接（带 30 秒缓存，同步兜底）。"""
    now = time.time()
    cached = _PROXY_CACHE.get(proxy)
    if cached and now - cached[0] < ttl:
        return cached[1]

    import socket

    ok = False
    try:
        host, port = _parse_proxy_host_port(proxy)
        with socket.create_connection((host, port), timeout=0.3):
            ok = True
    except OSError:
        ok = False

    _PROXY_CACHE[proxy] = (now, ok)
    return ok


async def _proxy_alive_async(proxy: str, ttl: float = 30.0) -> bool:
    """异步检查本地代理端口（不阻塞事件循环）。"""
    now = time.time()
    cached = _PROXY_CACHE.get(proxy)
    if cached and now - cached[0] < ttl:
        return cached[1]

    ok = False
    writer = None
    try:
        host, port = _parse_proxy_host_port(proxy)
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=0.3,
        )
        ok = True
    except (OSError, asyncio.TimeoutError):
        ok = False
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    _PROXY_CACHE[proxy] = (now, ok)
    return ok


def _get_or_create_client(effective: str | None) -> httpx.AsyncClient:
    key = effective or ""
    client = _HTTP_POOL.get(key)
    if client is None:
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(connect=20.0, read=600.0, write=60.0, pool=15.0),
            # 保活 5 分钟：httpx 默认只 5 秒，交互式使用两次请求间隔几乎必然超过 5 秒，
            # 导致每次都重新 TCP+TLS（走代理还要加 CONNECT）握手，实测首字多付 ~1.3s。
            "limits": httpx.Limits(
                max_connections=100, max_keepalive_connections=20, keepalive_expiry=300.0,
            ),
            # trust_env=False：避免 Windows 上 NO_PROXY 的 IPv6 写法导致 httpx 解析失败
            "trust_env": False,
        }
        if effective:
            kwargs["proxy"] = effective
        client = httpx.AsyncClient(**kwargs)
        _HTTP_POOL[key] = client
    return client


def http_client(proxy: str | None = None) -> httpx.AsyncClient:
    """按代理取（或创建）HTTP 客户端。proxy=None 表示直连。"""
    effective = proxy if (proxy and _proxy_alive(proxy)) else None
    return _get_or_create_client(effective)


async def async_http_client(proxy: str | None = None) -> httpx.AsyncClient:
    """异步取 HTTP 客户端（代理探测不阻塞事件循环）。"""
    effective = proxy if (proxy and await _proxy_alive_async(proxy)) else None
    return _get_or_create_client(effective)


async def close_http() -> None:
    for client in list(_HTTP_POOL.values()):
        try:
            await client.aclose()
        except Exception:
            pass
    _HTTP_POOL.clear()


# --------------------------------------------------------------------------
# 鉴权
# --------------------------------------------------------------------------

def extract_key(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            return token
    for header in ("x-api-key", "x-goog-api-key"):
        value = (request.headers.get(header) or "").strip()
        if value:
            return value
    return request.query_params.get("key") or None


def unauthorized(message: str = "Invalid API key") -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": "invalid_request_error", "code": "invalid_api_key"}},
        status_code=401,
    )


# --------------------------------------------------------------------------
# 用量解析
# --------------------------------------------------------------------------

def usage_from_openai(payload: dict[str, Any]) -> dict[str, int]:
    """解析 OpenAI 格式用量并归一化。

    重要：OpenAI 风格的 `prompt_tokens` 是**含缓存的总输入**，
    而 `prompt_tokens_details.cached_tokens` 是其中的子集。
    如果直接用 prompt_tokens 算输入费、再加 cache 费，会重复计费。

    这里统一成「输入 = 未命中缓存的部分」「cache_read = 命中的部分」，
    两者相加才是上游报告的总 prompt_tokens。
    """
    usage = payload.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    out_details = usage.get("output_tokens_details") or {}
    prompt_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}

    prompt_total = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)

    # 缓存读：不同供应商字段名不同
    cache_read = int(
        prompt_details.get("cached_tokens")
        or usage.get("prompt_cache_hit_tokens")      # DeepSeek 风格
        or usage.get("cache_read_input_tokens")      # 部分中转
        or 0
    )
    # 防御：缓存数不应超过总输入
    cache_read = max(0, min(cache_read, prompt_total))

    # 推理 token：OpenAI 放在 completion_tokens_details 里，
    # 但 axon 等中转直接把 reasoning_tokens 放在 usage 顶层，两种都要认。
    reasoning = int(
        details.get("reasoning_tokens")
        or out_details.get("reasoning_tokens")
        or usage.get("reasoning_tokens")             # axon / 部分中转
        or 0
    )

    completion_total = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0)
    # 少数上游返回的 completion_tokens 不含 reasoning_tokens，但 total_tokens 含两者；
    # 若 total_tokens > prompt_total + completion_total，补齐到真实总输出，避免少算生成速度与成本。
    if reasoning > 0 and total_tokens > prompt_total + completion_total:
        completion_total = max(completion_total, total_tokens - prompt_total)

    return {
        "input": prompt_total - cache_read,          # 只算未命中的部分
        "output": completion_total,
        "cache_read": cache_read,
        "cache_write": 0,
        "reasoning": reasoning,
    }


def usage_from_anthropic(payload: dict[str, Any]) -> dict[str, int]:
    usage = payload.get("usage") or {}
    return {
        "input": int(usage.get("input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
        "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
    }


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（上游不返回 usage 时兜底）。

    英文/ASCII 约 4 字符/token，CJK 约 1.5 字符/token。
    """
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other_chars = len(text) - ascii_chars
    return max(1, int(ascii_chars / 4 + other_chars / 1.5))


def estimate_prompt_tokens(body: bytes) -> int:
    """从请求体估算输入 token。"""
    try:
        payload = json.loads(body or b"{}")
    except (json.JSONDecodeError, TypeError):
        return 0

    chunks: list[str] = []
    for msg in payload.get("messages") or []:
        content = msg.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
        if isinstance(msg.get("role"), str):
            chunks.append(msg["role"])

    system = payload.get("system")
    if isinstance(system, str):
        chunks.append(system)

    return estimate_tokens("\n".join(chunks))


def _extract_stream_text(obj: dict[str, Any], sink: list[str]) -> None:
    """从 SSE 分片里累积助手输出文本（含正文、推理与工具调用参数），用于无 usage 时估算 token。"""
    # Anthropic /messages 流式增量
    if obj.get("type") in ("response.output_text.delta", "response.refusal.delta",
                           "response.reasoning_summary_text.delta", "response.function_call_arguments.delta"):
        delta = obj.get("delta")
        if isinstance(delta, str) and delta:
            sink.append(delta)
        return
    if obj.get("type") == "content_block_delta":
        delta = obj.get("delta")
        if isinstance(delta, dict):
            for key in ("text", "thinking", "partial_json"):
                val = delta.get(key)
                if isinstance(val, str) and val:
                    sink.append(val)
        return

    choices = obj.get("choices")
    if not isinstance(choices, list):
        return
    for ch in choices:
        if not isinstance(ch, dict):
            continue
        delta = ch.get("delta")
        if not isinstance(delta, dict):
            continue
        for key in ("content", "reasoning_content", "reasoning"):
            val = delta.get(key)
            if isinstance(val, str) and val:
                sink.append(val)
            elif isinstance(val, (list, dict)):
                txt = _part_text(val)
                if txt:
                    sink.append(txt)
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function")
                if isinstance(fn, dict):
                    for key in ("name", "arguments"):
                        val = fn.get(key)
                        if isinstance(val, str) and val:
                            sink.append(val)


def _obj_has_output_token(obj: dict[str, Any], path: str) -> bool:
    """判断一条 SSE JSON 帧是否携带真正的生成 token（排除空 role:assistant 预热帧）。"""
    if path.endswith("/responses"):
        return obj.get("type") in ("response.output_text.delta", "response.refusal.delta",
                                   "response.reasoning_summary_text.delta", "response.function_call_arguments.delta") and bool(obj.get("delta"))
    if "/messages" in path:
        etype = obj.get("type")
        if etype == "content_block_delta":
            delta = obj.get("delta")
            if isinstance(delta, dict):
                return any(bool(delta.get(k)) for k in ("text", "thinking", "partial_json"))
        if etype == "content_block_start":
            cb = obj.get("content_block")
            if isinstance(cb, dict) and cb.get("type") == "tool_use":
                return True
        return False

    choices = obj.get("choices")
    if not isinstance(choices, list):
        return False
    for ch in choices:
        if not isinstance(ch, dict):
            continue
        delta = ch.get("delta")
        if not isinstance(delta, dict):
            continue
        for key in ("content", "reasoning_content", "reasoning"):
            val = delta.get(key)
            if isinstance(val, str) and val:
                return True
            if isinstance(val, (list, dict)) and _part_text(val):
                return True
        tcs = delta.get("tool_calls")
        if isinstance(tcs, list) and tcs:
            return True
    return False


# --------------------------------------------------------------------------
# 响应兼容：content 块与结构归一化
# --------------------------------------------------------------------------
#
# 部分上游（实测 Mistral 的推理模型在带 reasoning_effort 时）在「OpenAI 兼容」
# 接口里返回的不是 delta.content: "text"，而是块数组：
#
#   "content": [{"type": "thinking",
#                "thinking": [{"type": "text", "text": "The"}],
#                "closed": true}]
#
# 客户端（ZCode 等）按 OpenAI schema 校验 content 必须是 string | null，
# 收到数组会直接判整轮失败（Turn execution failed）。网关必须在转发前压平：
#   - text 块     -> delta.content（字符串）
#   - thinking 块 -> delta.reasoning_content（字符串，客户端可实时展示思考过程）
# 同时修正常见上游结构毛刺：
#   - usage 帧 choices: null -> choices: []
#   - tool_calls 缺 index / arguments 为 dict / 续传帧 function.name 为空串 ""

_TEXT_PART_TYPES = {"text", "output_text"}
_REASONING_PART_TYPES = {"thinking", "reasoning"}


def _part_text(part: Any) -> str:
    """从单个内容块/子块里取出纯文本，兼容嵌套的 thinking 结构和块数组。"""
    result: list[str] = []
    stack = [part]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, list):
            stack.extend(reversed(item))
        elif isinstance(item, dict):
            for key in ("text", "thinking", "content", "reasoning"):
                value = item.get(key)
                if isinstance(value, str):
                    result.append(value)
                    break
                if isinstance(value, list):
                    stack.extend(reversed(value))
                    break
    return "".join(result)


def _split_content_parts(parts: list[Any]) -> tuple[str, str]:
    """把块数组拆成 (正文, 推理)。未知类型的块按正文处理（多数是 text）。"""
    text: list[str] = []
    reasoning: list[str] = []
    for part in parts:
        ptype = part.get("type") if isinstance(part, dict) else None
        if isinstance(ptype, str) and ptype in _REASONING_PART_TYPES:
            reasoning.append(_part_text(part))
        elif ptype is None or isinstance(ptype, str) and ptype in _TEXT_PART_TYPES:
            text.append(_part_text(part))
        # 其它类型（image_url 等）在流式增量里没有文本可提取，直接跳过
    return "".join(text), "".join(reasoning)


def _normalize_openai_payload(payload: Any) -> bool:
    """把非标 OpenAI 响应压平成严格兼容结构（原地修改）。

    返回是否发生了改动；没有改动时调用方直接透传原始字节，避免额外序列化。
    """
    if not isinstance(payload, dict):
        return False

    # Validate every structure we might normalize before modifying anything.
    # Nested content arrays are unusual and intentionally passed through.
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                return False
            for holder in ("delta", "message"):
                obj = choice.get(holder)
                if obj is None:
                    continue
                if not isinstance(obj, dict):
                    return False
                role = obj.get("role")
                if role is not None and not isinstance(role, str):
                    return False
                for key in ("content", "reasoning_content", "reasoning"):
                    value = obj.get(key)
                    if value is None or isinstance(value, str):
                        continue
                    if not isinstance(value, (dict, list)):
                        return False
                    stack = [value]
                    while stack:
                        part = stack.pop()
                        if isinstance(part, str):
                            continue
                        if isinstance(part, list):
                            if any(isinstance(child, list) for child in part):
                                return False
                            stack.extend(part)
                        elif isinstance(part, dict):
                            ptype = part.get("type")
                            if ptype is not None and not isinstance(ptype, str):
                                return False
                            for field in ("text", "thinking", "content", "reasoning"):
                                if field in part:
                                    child = part[field]
                                    if not isinstance(child, (str, list)):
                                        return False
                                    stack.append(child)
                        else:
                            return False
                tools = obj.get("tool_calls")
                if tools is not None:
                    if not isinstance(tools, list):
                        return False
                    for tool in tools:
                        if not isinstance(tool, dict):
                            return False
                        fn = tool.get("function")
                        if fn is not None and not isinstance(fn, dict):
                            return False
    elif choices is not None:
        return False

    changed = False
    if choices is None and "error" not in payload and ("usage" in payload or "object" in payload):
        payload["choices"] = []
        choices = payload["choices"]
        changed = True

    if not isinstance(choices, list):
        return changed

    for choice in choices:
        if not isinstance(choice, dict):
            continue
        for holder in ("delta", "message"):
            obj = choice.get(holder)
            if not isinstance(obj, dict):
                continue

            # delta.role 在严格客户端只允许 "assistant" / "" / null
            if holder == "delta" and "role" in obj:
                role_val = obj.get("role")
                if role_val is not None and role_val not in ("assistant", ""):
                    obj["role"] = "assistant"
                    changed = True

            content = obj.get("content")
            reasoning = ""
            if isinstance(content, list):
                text, reasoning = _split_content_parts(content)
                obj["content"] = text
                changed = True
            elif isinstance(content, dict):
                text, reasoning = _split_content_parts([content])
                obj["content"] = text
                changed = True

            # 有些上游把推理放在顶层 reasoning / reasoning_content 且也是数组/对象
            for key in ("reasoning_content", "reasoning"):
                val = obj.get(key)
                if isinstance(val, (list, dict)):
                    flattened = _part_text(val)
                    obj[key] = flattened
                    changed = True
                    if key == "reasoning_content":
                        reasoning = reasoning or flattened

            if reasoning:
                prev = obj.get("reasoning_content")
                if isinstance(prev, (list, dict)):
                    prev = _part_text(prev)
                base = prev if isinstance(prev, str) else ""
                if base != reasoning:
                    obj["reasoning_content"] = base + reasoning
                    changed = True

            # tool_calls 结构规范化
            tool_calls = obj.get("tool_calls")
            if isinstance(tool_calls, list):
                for idx, tc in enumerate(tool_calls):
                    if not isinstance(tc, dict):
                        continue
                    if holder == "delta" and tc.get("index") is None:
                        tc["index"] = idx
                        changed = True
                    fn = tc.get("function")
                    if isinstance(fn, dict):
                        args = fn.get("arguments")
                        if args is not None and not isinstance(args, str):
                            try:
                                fn["arguments"] = json.dumps(args, ensure_ascii=False)
                                changed = True
                            except (TypeError, ValueError):
                                pass
                        # Mistral 等在 tool_calls 后续分片里会发 "name": ""（空串），
                        # 严格客户端判断 name != null 或拼接 name 时可能异常，空串应置为 None
                        if holder == "delta" and "name" in fn and fn.get("name") == "":
                            fn["name"] = None
                            changed = True
    return changed


def _is_openai_chat_path(path: str) -> bool:
    """只归一化 OpenAI 风格的接口，Anthropic / Gemini 的数组格式本身就是合法的。"""
    if "/messages" in path:
        return False
    return path.endswith(("/chat/completions", "/completions", "/responses"))


def _normalize_sse_line(line: bytes, path: str) -> bytes:
    """归一化单条 SSE 行；不是 data 行或无需改动时原样返回（保持同一引用）。"""
    if not _is_openai_chat_path(path) or path.endswith("/responses"):
        return line
    stripped = line.strip()
    if not stripped.startswith(b"data:"):
        return line
    data = stripped[5:].strip()
    if not data or data == b"[DONE]":
        return line
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return line
    if not _normalize_openai_payload(payload):
        return line
    try:
        return b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return line


def _ingest_sse_obj(
    obj: dict[str, Any],
    path: str,
    collected: dict[str, Any] | None,
    text_sink: list[str] | None,
    first_token_at: list[float] | None,
) -> None:
    """从已解析的单条 SSE JSON 对象提取模型名、真实首字时间、输出文本和 usage。"""
    if first_token_at is not None and not first_token_at:
        if _obj_has_output_token(obj, path):
            first_token_at.append(time.perf_counter())

    if text_sink is not None:
        _extract_stream_text(obj, text_sink)

    if collected is None:
        return

    if path.endswith("/responses") and isinstance(obj.get("response"), dict):
        obj = obj["response"]
    if isinstance(obj.get("model"), str) and obj["model"]:
        collected["model"] = obj["model"]

    if "/messages" in path:
        if obj.get("type") == "message_start":
            msg = obj.get("message") or {}
            if isinstance(msg.get("model"), str) and msg["model"]:
                collected["model"] = msg["model"]
            u = msg.get("usage") or {}
            if u:
                collected["usage"] = usage_from_anthropic({"usage": u})
        elif obj.get("type") == "message_delta":
            u = obj.get("usage") or {}
            if u:
                prev = collected.get("usage") or {}
                merged = usage_from_anthropic({"usage": u})
                for k, v in merged.items():
                    if v:
                        prev[k] = v
                collected["usage"] = prev
    else:
        u = obj.get("usage")
        if isinstance(u, dict) and u:
            parsed = usage_from_openai({"usage": u})
            if any(parsed.values()):
                collected["usage"] = parsed
            c = upstream_cost({"usage": u})
            if c is not None:
                collected["cost"] = c


def _process_sse_line(
    raw_line: bytes,
    path: str,
    collected: dict[str, Any] | None = None,
    text_sink: list[str] | None = None,
    first_token_at: list[float] | None = None,
) -> bytes:
    """单次 JSON 解析同时完成：响应兼容归一化 + 用量/首字/文本提取。"""
    line = raw_line[:-1] if raw_line.endswith(b"\r") else raw_line
    stripped = line.strip()
    if not stripped.startswith(b"data:"):
        return line
    data = stripped[5:].strip()
    if not data or data == b"[DONE]":
        return line
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return line
    if not isinstance(payload, dict):
        return line

    changed = _is_openai_chat_path(path) and not path.endswith("/responses") and _normalize_openai_payload(payload)
    _ingest_sse_obj(payload, path, collected, text_sink, first_token_at)

    if not changed:
        return line
    try:
        return b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return line


async def _iter_normalized_sse(
    resp: httpx.Response,
    path: str,
    collected: dict[str, Any] | None = None,
    text_sink: list[str] | None = None,
    first_token_at: list[float] | None = None,
    terminal: dict[str, Any] | None = None,
):
    """按完整行缓冲并按网络分片批量转发上游 SSE。

    - 解决 JSON 跨 TCP 包被截断导致丢帧或漏记 usage 的问题；
    - 同一个网络包内的多行合并一次 yield，避免逐行触发 ASGI send 系统调用；
    - 单次 JSON 解析同时完成兼容改写与用量采集。
    """
    buf = b""
    passthrough_line = False
    max_line_bytes = 1024 * 1024
    parser = SSEParser() if terminal is not None else None
    parser_pending_cr = False

    def observe(blocks):
        for block in blocks:
            if not block.dispatch:
                continue
            data = block.data
            if data.strip() == "[DONE]" and _is_openai_chat_path(path) and not path.endswith("/responses"):
                terminal["done"] = True
                continue
            try:
                obj = json.loads(data)
            except (ValueError, RecursionError):
                obj = None
            if block.event == "error" or (isinstance(obj, dict) and
                    (obj.get("type") == "error" or obj.get("error"))):
                terminal["error"] = True
            if "/messages" in path and (block.event == "message_stop" or
                    isinstance(obj, dict) and obj.get("type") == "message_stop"):
                terminal["done"] = True
            if path.endswith("/responses"):
                event_type = obj.get("type") if isinstance(obj, dict) else None
                if block.event in ("response.failed", "response.incomplete") or event_type in ("response.failed", "response.incomplete"):
                    terminal["error"] = True
                if block.event == "response.completed" or event_type == "response.completed":
                    terminal["done"] = True
            if isinstance(obj, dict):
                try:
                    _ingest_sse_obj(obj, path, collected, text_sink, first_token_at)
                except (ValueError, TypeError, AttributeError, OverflowError):
                    # Malformed optional accounting must never break passthrough.
                    pass

    async for chunk in resp.aiter_bytes():
        if parser is not None:
            # A failing feed cannot return already parsed blocks. Feed through
            # each line boundary separately so later oversized input cannot
            # discard earlier complete observations from the same network chunk.
            start = 0
            try:
                for match in re.finditer(b"[\r\n]", chunk):
                    end = match.end()
                    if parser_pending_cr:
                        if chunk[start:start + 1] == b"\n":
                            observe(parser.feed(chunk[start:start + 1]))
                            start += 1
                        elif start < match.start():
                            observe(parser.feed(chunk[start:start + 1]))
                            start += 1
                        parser_pending_cr = False
                    if end <= start:
                        continue
                    # Resolve a preceding CR independently before a potentially
                    # oversized following line; the first byte cannot exceed a
                    # fresh block's limit after a completed blank line.
                    if start < match.start():
                        observe(parser.feed(chunk[start:match.start()]))
                    observe(parser.feed(chunk[match.start():end]))
                    parser_pending_cr = chunk[match.start():end] == b"\r"
                    start = end
                if start < len(chunk):
                    if parser_pending_cr:
                        observe(parser.feed(chunk[start:start + 1]))
                        start += 1
                        parser_pending_cr = False
                    if start < len(chunk):
                        observe(parser.feed(chunk[start:]))
            except SSELimitError:
                terminal["unobservable"] = True
                parser = None
        buf += chunk
        output = bytearray()
        start = 0
        for match in re.finditer(b"[\\r\\n]", buf):
            line = buf[start:match.start()]
            if passthrough_line or len(line) >= max_line_bytes:
                output.extend(line)
            else:
                output.extend(_process_sse_line(
                    line, path, collected if terminal is None else None,
                    text_sink if terminal is None else None,
                    first_token_at if terminal is None else None))
            output.extend(match.group())
            passthrough_line = False
            start = match.end()
        buf = buf[start:]
        if len(buf) >= max_line_bytes:
            output.extend(buf)
            buf = b""
            passthrough_line = True
        if output:
            yield bytes(output)
    if parser is not None:
        observe(parser.finish())
    if buf:
        yield buf if passthrough_line else _process_sse_line(
            buf, path, collected if terminal is None else None,
            text_sink if terminal is None else None,
            first_token_at if terminal is None else None)


def upstream_cost(payload: dict[str, Any]) -> float | None:
    """提取上游自己算好的成本（USD）。

    优先级：
      1. usage.opencode.list_cost      —— Zen 按官方价算的成本（最准）
      2. usage.cost                    —— 部分上游直接返回
      3. usage.opencode.allowance      —— Zen 实际扣的额度
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None

    zen = usage.get("opencode")
    if isinstance(zen, dict):
        for key in ("list_cost", "allowance"):
            val = zen.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return float(val)

    cost = usage.get("cost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return float(cost)

    return None


def record_from_usage(
    *,
    source: str,
    model: str | None,
    requested_model: str | None,
    client_key: str | None,
    usage: dict[str, int],
    ts: int,
    latency_ms: float | None,
    status: int,
    request_id: str,
    prices: PriceTable | None = None,
    upstream_cost_usd: float | None = None,
    estimated: bool = False,
    ttft_ms: float | None = None,
) -> UsageRecord:
    """构造用量记录。

    upstream_cost_usd 有值时优先采用（上游自己算的，比本地按价格表推算更准）。
    estimated=True 表示 token 是本地估算的（上游没返回 usage）。
    ttft_ms 是首字延迟 —— 流式路径算出来后必须传进来，
    否则 /api/speed 无法算纯解码速度，只能拿含等待的总耗时糊弄。
    """
    cost = upstream_cost_usd
    if cost is None and prices is not None:
        cost = prices.compute(
            upstream=source,
            model=model,
            input_tokens=usage.get("input", 0),
            output_tokens=usage.get("output", 0),
            cache_read=usage.get("cache_read", 0),
            cache_write=usage.get("cache_write", 0),
            at=datetime.fromtimestamp(ts, tz=timezone.utc),
        )
    return UsageRecord(
        ts=ts,
        source=source,
        model=model,
        requested_model=requested_model,
        client_key=client_key,
        input_tokens=usage.get("input", 0),
        output_tokens=usage.get("output", 0),
        cache_read=usage.get("cache_read", 0),
        cache_write=usage.get("cache_write", 0),
        reasoning=usage.get("reasoning", 0),
        cost_usd=cost,
        latency_ms=latency_ms,
        ttft_ms=ttft_ms,
        status=status,
        raw={
            "request_id": request_id,
            **({"estimated": True} if estimated else {}),
        },
    )


# --------------------------------------------------------------------------
# 转发
# --------------------------------------------------------------------------

# 可重试的状态码（同时也是计入上游健康度熔断的服务端/过载状态码；
# 400/401/403/404/422 属于请求参数/客户端错误，绝不触发上游冷却熔断）
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}

# key 额度耗尽/被限流的冷却时长：期间优先用下一个 key，到期后再试主 key
KEY_COOLDOWN_SEC = 900
_KEY_COOLDOWN: dict[str, float] = {}


def _key_id(up: Upstream, key: str) -> str:
    return f"{up.name}|{key[-6:]}"


def _key_chain(up: Upstream) -> list[str]:
    """按优先级取 key：跳过冷却中的；全部冷却时退回主 key 再试一次。"""
    keys = up.effective_keys
    now = time.time()
    fresh = [k for k in keys if _KEY_COOLDOWN.get(_key_id(up, k), 0.0) <= now]
    return fresh or keys[:1]


def _conversation_key(
    body: bytes, client_key: str, payload: dict[str, Any] | None = None,
) -> str:
    """从对话开头推导一个稳定的 cache 路由键。

    Zen 用 x-opencode-session 做 prompt cache 的分区键：
      - 每个请求换新 session = 缓存永不命中（贵 20~30 倍）
      - 所有对话共用一个 session = 多个对话互相把对方的缓存冲掉
    取「第一条 user 消息」的哈希：它在一个会话里永不改变，
    不同会话天然不同（首条消息里带着各自的运行上下文），
    所以既能连续命中，又不会跨会话互踩。
    """
    if payload is None:
        try:
            payload = json.loads(body or b"{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
    msgs = payload.get("messages") if isinstance(payload, dict) else None
    anchor = ""
    if isinstance(msgs, list):
        picked = None
        for msg in msgs:
            if isinstance(msg, dict) and msg.get("role") == "user":
                picked = msg
                break
        if picked is None and msgs and isinstance(msgs[0], dict):
            picked = msgs[0]
        if picked is not None:
            content = picked.get("content")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text") or "" for p in content
                    if isinstance(p, dict) and isinstance(p.get("text"), str)
                )
            anchor = content if isinstance(content, str) else ""
    if not anchor:
        return f"gw-{client_key or 'default'}"
    digest = hashlib.sha1(anchor[:800].encode("utf-8", "ignore")).hexdigest()[:10]
    return f"gw-{client_key or 'default'}-{digest}"


def _build_headers(
    request: Request,
    upstream: Upstream,
    request_id: str,
    client_key: str = "",
    body: bytes = b"",
    payload: dict[str, Any] | None = None,
    path: str = "",
    api_key_override: str | None = None,
) -> dict[str, str]:
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in {
            "host", "authorization", "x-api-key", "x-goog-api-key", "content-length", "x-session-id",
            "accept-encoding",  # 由 httpx 自己协商，避免解压错乱
        }
    }
    api_key = api_key_override if api_key_override is not None else upstream.api_key
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
        if "/messages" in path:
            headers["x-api-key"] = api_key
            if not any(k.lower() == "anthropic-version" for k in headers):
                headers["anthropic-version"] = "2023-06-01"

    # OpenCode Zen 要求 x-opencode-session 头用于路由和 prompt 缓存；
    # 同时必须覆盖 User-Agent —— 客户端的 UA（如 Python-urllib）
    # 会被 Cloudflare 判定为异常流量并返回挑战页。
    if "opencode.ai" in upstream.base_url:
        incoming = headers.get("x-opencode-session")
        if incoming:
            headers["x-opencode-session"] = incoming
        else:
            headers["x-opencode-session"] = _conversation_key(body, client_key, payload=payload)
        headers["user-agent"] = "usage-gateway/0.1.0"
    else:
        if not headers.get("user-agent"):
            headers["user-agent"] = "usage-gateway/0.1.0"

    return headers


def _model_supports_reasoning_effort(model: str, patterns: list[str]) -> bool:
    from fnmatch import fnmatch

    m = (model or "").lower()
    return any(fnmatch(m, str(pat).lower()) for pat in patterns)


def _normalize_request_body(
    body: bytes,
    upstream: Upstream,
    *,
    payload: dict[str, Any] | None = None,
    path: str = "/chat/completions",
    use_stream: bool | None = None,
    force_strip_reasoning_effort: bool = False,
    model_override: str | None = None,
    reuse_payload: bool = False,
) -> bytes:
    """按上游的兼容性改写请求体。

    支持：
      - developer 角色改写为 system
      - 流式请求自动补齐 stream_options: {"include_usage": True}（确保拿到真实 token）
      - reasoning_effort 枚举映射（如 Mistral 不认 "medium"，映射为 "high"）与非推理模型自动剥离
      - 顶层字段改名 / 黑名单 / 白名单（allow_params）
      - 历史消息深度清理：剥离 strip_message_fields、content[].cache_control、
        tool_calls[].extra_content（如 Gemini thought_signature 跨模型切换时导致 Mistral 422）
      - force_non_stream 时自动降级 stream=False
    """
    if payload is None or not reuse_payload:
        try:
            payload = json.loads(body or b"{}")
        except (json.JSONDecodeError, TypeError):
            return body
    if not isinstance(payload, dict):
        return body

    compat = upstream.compat or {}
    changed = False

    if model_override and payload.get("model") != model_override:
        payload["model"] = model_override
        changed = True

    # 0. 流式降级或自动注入 include_usage
    client_stream = bool(payload.get("stream"))
    if use_stream is False and client_stream:
        payload["stream"] = False
        payload.pop("stream_options", None)
        changed = True
    elif (
        (use_stream or (use_stream is None and client_stream))
        and path.endswith("/chat/completions")
        and compat.get("inject_include_usage", True)
    ):
        allow_list = compat.get("allow_params")
        if not allow_list or "stream_options" in allow_list:
            so = payload.get("stream_options")
            if not isinstance(so, dict):
                payload["stream_options"] = {"include_usage": True}
                changed = True
            elif not so.get("include_usage"):
                so["include_usage"] = True
                changed = True

    # 1. developer 角色改写
    role_mode = str(compat.get("developer_role") or "system")
    if role_mode != "keep":
        messages = payload.get("messages")
        if isinstance(messages, list):
            for msg in messages:
                if isinstance(msg, dict) and msg.get("role") == "developer":
                    msg["role"] = role_mode
                    changed = True

    # 2. 顶层字段改名（目标已存在则直接丢弃源字段）
    rename = {str(k): str(v) for k, v in (compat.get("rename_params") or {}).items()}
    for src, dst in rename.items():
        if src not in payload:
            continue
        value = payload.pop(src)
        changed = True
        if dst not in payload:
            payload[dst] = value

    # 3. 顶层字段丢弃
    for key in (compat.get("drop_params") or []):
        if str(key) in payload:
            payload.pop(str(key))
            changed = True

    # 3.2 reasoning_effort 兼容处理
    if force_strip_reasoning_effort and "reasoning_effort" in payload:
        payload.pop("reasoning_effort", None)
        changed = True
    elif "reasoning_effort" in payload:
        eff = payload.get("reasoning_effort")
        if eff is None:
            payload.pop("reasoning_effort", None)
            changed = True
        else:
            model_name = str(payload.get("model") or "")
            reasoning_models = compat.get("reasoning_models")
            if isinstance(reasoning_models, list) and reasoning_models:
                if not _model_supports_reasoning_effort(model_name, reasoning_models):
                    payload.pop("reasoning_effort", None)
                    changed = True
            if "reasoning_effort" in payload:
                eff_map = compat.get("reasoning_effort_map") or {}
                if isinstance(eff_map, dict) and str(eff) in eff_map:
                    mapped = eff_map[str(eff)]
                    if mapped is None or mapped == "":
                        payload.pop("reasoning_effort", None)
                    else:
                        payload["reasoning_effort"] = mapped
                    changed = True

    # 3.5 白名单：配了 allow_params 就只保留这些顶层字段。
    allow = [str(x) for x in (compat.get("allow_params") or [])]
    if allow:
        allowed = set(allow) | {"model", "messages"}
        for key in [k for k in payload if k not in allowed]:
            payload.pop(key)
            changed = True

    # 4. 消息与嵌套结构清理
    strip_fields = {str(x) for x in (compat.get("strip_message_fields") or [])}
    strict_schema = bool(allow or strip_fields or compat.get("strict_messages"))
    if strip_fields or strict_schema:
        messages = payload.get("messages")
        if isinstance(messages, list):
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                is_tool = role == "tool"
                for field in strip_fields:
                    if field not in msg:
                        continue
                    if is_tool and field == "name":
                        continue
                    msg.pop(field)
                    changed = True

                # 清理 content 块上的非标字段（如 Anthropic 的 cache_control）
                content = msg.get("content")
                if strict_schema and isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and "cache_control" in part:
                            part.pop("cache_control", None)
                            changed = True

                # 清理 assistant.tool_calls 上的跨渠道残留字段（如 Google extra_content）
                if strict_schema and "tool_calls" in msg:
                    tcs = msg.get("tool_calls")
                    if isinstance(tcs, list):
                        if not tcs:
                            msg.pop("tool_calls", None)
                            changed = True
                        else:
                            for tc in tcs:
                                if not isinstance(tc, dict):
                                    continue
                                for extra_k in [k for k in tc if k not in {"id", "type", "function"}]:
                                    tc.pop(extra_k, None)
                                    changed = True
                                fn = tc.get("function")
                                if isinstance(fn, dict):
                                    for extra_fk in [k for k in fn if k not in {"name", "arguments"}]:
                                        fn.pop(extra_fk, None)
                                        changed = True
                                    if "arguments" in fn and not isinstance(fn["arguments"], str):
                                        try:
                                            fn["arguments"] = json.dumps(fn["arguments"], ensure_ascii=False)
                                        except (TypeError, ValueError):
                                            fn["arguments"] = "{}"
                                        changed = True

                # assistant 消息若剥完 reasoning_content 后既无 content 也无 tool_calls，补空串避免 422
                if strict_schema and role == "assistant":
                    if msg.get("content") is None and not msg.get("tool_calls"):
                        msg["content"] = ""
                        changed = True

    # 5. max_tokens: null 对严格上游是类型错误
    if "max_tokens" in payload and payload["max_tokens"] is None:
        payload.pop("max_tokens")
        changed = True

    if not changed:
        return body
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _detect_stream(path: str, body: bytes, payload: dict[str, Any] | None = None) -> bool:
    if "streamGenerateContent" in path:
        return True
    if path.endswith(("/chat/completions", "/completions", "/messages", "/responses")):
        if isinstance(payload, dict):
            return bool(payload.get("stream"))
        try:
            return bool(json.loads(body or b"{}").get("stream"))
        except (json.JSONDecodeError, AttributeError, TypeError):
            return False
    return False


def _extract_usage(payload: dict[str, Any], path: str) -> dict[str, int]:
    if "/messages" in path:
        return usage_from_anthropic(payload)
    return usage_from_openai(payload)


async def forward_with_failover(
    *,
    request: Request,
    cfg: Config,
    store: Store,
    tracker: Any,
    path: str,
    body: bytes,
    client_key_name: str,
    model: str | None,
    requested_model: str,
    candidates: list[Upstream],
    allow_retry: bool = True,
    max_rounds: int = 1,
    parsed_payload: dict[str, Any] | None = None,
) -> Response:
    """按候选顺序尝试上游，失败自动切换。

    candidates   候选上游列表（同别名可配多个，形成分组）
    max_rounds   轮数：一轮走完全部候选；>1 时失败会再走一轮（重试）

    非流式：网络错误或 5xx/429 时换下一个上游；4xx 直接透传不重试。
    流式：拿到响应头之前可切换；已开始推流则不再切换。
    """
    request_id = uuid.uuid4().hex
    trace = Trace(request_id)
    trace.start("receive", client_key=client_key_name, model=requested_model)

    if parsed_payload is None and body:
        try:
            loaded = json.loads(body)
            if isinstance(loaded, dict):
                parsed_payload = loaded
        except (json.JSONDecodeError, TypeError):
            parsed_payload = None

    client_wants_stream = _detect_stream(path, body, payload=parsed_payload)
    is_stream = client_wants_stream
    ts = int(time.time())
    attempted: list[str] = []
    last_error: str = ""
    last_status: int = 502
    last_trace: dict | None = None

    trace.start("route", candidates=[u.name for u in candidates],
                stream=is_stream, rounds=max_rounds)

    # 构造尝试队列：每轮走一遍全部候选
    queue: list[Upstream] = []
    for _ in range(max(1, max_rounds)):
        queue.extend(candidates)

    # 只有存在多个不同候选上游可切换时，才因 cooldown 跳过；
    # 若用户指定了唯一上游（如严格模式 failover=False），绝不因为本地冷却记录直接返回 503，
    # 而是直接尝试上游（避免瞬时抖动或误判把渠道锁死几分钟）。
    unique_candidates = {u.name for u in candidates}

    for idx, upstream in enumerate(queue):
        if (
            len(unique_candidates) > 1
            and tracker is not None
            and not tracker.is_available(upstream.name)
        ):
            trace.add("skip", 0, upstream=upstream.name, reason="cooldown")
            continue

        # 该上游不支持流式时，改走非流式，稍后包装成 SSE
        use_stream = client_wants_stream and not upstream.force_non_stream

        req_body = _normalize_request_body(
            body,
            upstream,
            payload=parsed_payload,
            path=path,
            use_stream=use_stream,
            model_override=model,
            reuse_payload=(idx == 0 and len(queue) == 1),
        )

        remaining = len(queue) - idx - 1
        attempted.append(upstream.name)
        url = f"{upstream.base_url}{path}"
        key_chain = _key_chain(upstream)
        headers = _build_headers(
            request, upstream, request_id, client_key_name, req_body,
            payload=parsed_payload, path=path,
            api_key_override=key_chain[0],
        )
        client = await async_http_client(cfg.proxy_for(upstream))
        started = time.perf_counter()

        trace.start("upstream", target=upstream.name, attempt=idx + 1)

        try:
            if use_stream:
                req = client.build_request(
                    request.method, url, headers=headers, content=req_body,
                    params=request.query_params,
                )
                resp = await client.send(req, stream=True)
            else:
                resp = await client.request(
                    request.method, url, headers=headers, content=req_body,
                    params=request.query_params,
                )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
            last_error = type(exc).__name__
            last_status = 504 if isinstance(exc, httpx.TimeoutException) else 502
            trace.end_current(error=last_error)
            if tracker is not None:
                tracker.record_failure(upstream.name, last_error)
            can_retry = allow_retry and remaining > 0
            log.warning(
                "[%s] attempt %d/%d %s failed: %s%s",
                request_id[:8], idx + 1, len(queue), upstream.name, last_error[:100],
                " -> failover" if can_retry else "",
            )
            if can_retry:
                continue
            break
        except httpx.HTTPError as exc:
            last_error = type(exc).__name__
            last_status = 502
            trace.end_current(error=last_error)
            if tracker is not None:
                tracker.record_failure(upstream.name, last_error)
            if allow_retry and remaining > 0:
                continue
            break

        # 拿到响应了
        latency = (time.perf_counter() - started) * 1000
        status = resp.status_code

        # 额度耗尽/被限流（402/429）：同一上游换下一个 key 重发（不算跨上游 failover）
        tried_keys = {key_chain[0]}
        key_retry_failed = False
        while status in (402, 429):
            nxt = next((k for k in _key_chain(upstream) if k not in tried_keys), None)
            if nxt is None:
                break
            _KEY_COOLDOWN[_key_id(upstream, key_chain[0])] = time.time() + KEY_COOLDOWN_SEC
            log.warning(
                "[%s] %s key hit %d, switching key (cooldown %ds)",
                request_id[:8], upstream.name, status, KEY_COOLDOWN_SEC,
            )
            try:
                if use_stream:
                    await resp.aclose()
                else:
                    await resp.aread()
                    await resp.aclose()
            except Exception:
                pass
            tried_keys.add(nxt)
            key_chain = [nxt] + [k for k in key_chain if k != nxt]
            headers = _build_headers(
                request, upstream, request_id, client_key_name, req_body,
                payload=parsed_payload, path=path, api_key_override=nxt,
            )
            started = time.perf_counter()
            try:
                if use_stream:
                    req = client.build_request(
                        request.method, url, headers=headers, content=req_body,
                        params=request.query_params,
                    )
                    resp = await client.send(req, stream=True)
                else:
                    resp = await client.request(
                        request.method, url, headers=headers, content=req_body,
                        params=request.query_params,
                    )
                status = resp.status_code
                latency = (time.perf_counter() - started) * 1000
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
                last_status = 504 if isinstance(exc, httpx.TimeoutException) else 502
                trace.end_current(error=last_error)
                if tracker is not None:
                    tracker.record_failure(upstream.name, last_error)
                key_retry_failed = True
                break

        if key_retry_failed:
            if allow_retry and remaining > 0:
                continue
            break

        # 自愈：如果上游因不支持 reasoning_effort 返回 400/422，自动剥离 reasoning_effort 就地重发一次
        if status in (400, 422) and b"reasoning_effort" in req_body:
            err_bytes = b""
            try:
                err_bytes = await resp.aread() if use_stream else (resp.content or b"")
            except Exception:
                pass
            if b"reasoning_effort" in err_bytes:
                try:
                    await resp.aclose()
                except Exception:
                    pass
                log.info(
                    "[%s] %s/%s rejected reasoning_effort (%d), stripping and retrying once",
                    request_id[:8], upstream.name, model or requested_model, status,
                )
                req_body = _normalize_request_body(
                    body,
                    upstream,
                    payload=parsed_payload,
                    path=path,
                    use_stream=use_stream,
                    force_strip_reasoning_effort=True,
                    model_override=model,
                    reuse_payload=False,
                )
                started = time.perf_counter()
                try:
                    if use_stream:
                        req = client.build_request(
                            request.method, url, headers=headers, content=req_body,
                            params=request.query_params,
                        )
                        resp = await client.send(req, stream=True)
                    else:
                        resp = await client.request(
                            request.method, url, headers=headers, content=req_body,
                            params=request.query_params,
                        )
                    latency = (time.perf_counter() - started) * 1000
                    status = resp.status_code
                except httpx.HTTPError:
                    pass

        # 5xx / 429 可以换上游重试
        if status in RETRYABLE_STATUS and allow_retry and remaining > 0:
            try:
                await resp.aclose()
            except Exception:
                pass
            last_error = f"HTTP {status}"
            last_status = status
            trace.end_current(error=last_error)
            if tracker is not None:
                tracker.record_failure(upstream.name, last_error)
            log.warning(
                "[%s] %s returned %d, trying next (%d left)",
                request_id[:8], upstream.name, status, remaining,
            )
            continue

        # 健康度记录：只有成功（<400）或服务端/过载错误（RETRYABLE_STATUS / >=500）才更新熔断器；
        # 400/404/422 等客户端参数错误不计入上游宕机次数，防止把正常渠道误熔断成 503。
        if tracker is not None:
            if status < 400:
                tracker.record_success(upstream.name, latency)
            elif status in RETRYABLE_STATUS or status >= 500:
                tracker.record_failure(upstream.name, f"HTTP {status}")
        trace.end_current()

        if use_stream:
            if status >= 400:
                try:
                    await resp.aread()
                except Exception:
                    pass
                try:
                    await resp.aclose()
                except Exception:
                    pass
                return await _non_stream_response(
                    resp=resp, request_id=request_id, trace=trace, store=store,
                    cfg=cfg, upstream=upstream, model=model,
                    requested_model=requested_model, client_key_name=client_key_name,
                    ts=ts, latency_ms=latency, path=path, status=status,
                    req_body=req_body,
                )
            return _stream_response(
                resp=resp, request_id=request_id, trace=trace, store=store,
                cfg=cfg, upstream=upstream, model=model,
                requested_model=requested_model, client_key_name=client_key_name,
                ts=ts, started=started, path=path, body=body,
            )

        # 客户端要流式但上游走了 force_non_stream：仅当 status < 400 时才包装成 SSE，
        # 错误状态码必须原样走 _non_stream_response 透传给客户端。
        if client_wants_stream and not use_stream and status < 400:
            return await _fake_stream_response(
                resp=resp, request_id=request_id, trace=trace, store=store,
                cfg=cfg, upstream=upstream, model=model,
                requested_model=requested_model, client_key_name=client_key_name,
                ts=ts, latency_ms=latency, path=path, status=status,
            )

        return await _non_stream_response(
            resp=resp, request_id=request_id, trace=trace, store=store,
            cfg=cfg, upstream=upstream, model=model,
            requested_model=requested_model, client_key_name=client_key_name,
            ts=ts, latency_ms=latency, path=path, status=status,
            req_body=req_body,
        )

    # 所有上游都失败
    trace.start("give_up", attempted=attempted, last_error=last_error[:200])
    last_trace = trace.finish()
    if not attempted:
        # 所有候选都在冷却中
        reason = (
            f"Upstream '{candidates[0].name if candidates else '?'}' is in cooldown "
            f"after repeated failures. Wait for it to recover, or reset via "
            f"POST /api/upstreams/{candidates[0].name if candidates else '?'}/reset"
        )
        status_out = 503
    else:
        reason = (
            f"Upstream '{attempted[-1]}' failed for model '{requested_model}': "
            f"{last_error[:300]}"
        )
        status_out = last_status

    log.error("[%s] request failed: %s", request_id[:8], reason[:200])

    src_name = attempted[0] if attempted else (candidates[0].name if candidates else "unknown")
    fail_rec = UsageRecord(
        ts=ts, source=src_name,
        model=model, requested_model=requested_model,
        client_key=client_key_name, status=status_out,
        raw={"request_id": request_id, "attempted": attempted,
             "error": last_error, "trace": last_trace},
    )
    await asyncio.to_thread(
        store.record_request,
        usage_record=fail_rec,
        trace=last_trace,
        ts=ts,
        source=src_name,
        model=model,
        requested_model=requested_model,
        client_key=client_key_name,
        status=status_out,
        result="timeout" if last_status == 504 else "upstream_error",
        error_stage="upstream",
        error_kind=last_error or "cooldown",
        error_status=status_out,
    )

    return JSONResponse(
        {
            "error": {
                "message": reason,
                "type": "upstream_error",
                "trace_id": request_id,
                "attempted": attempted,
                "failover_enabled": bool(cfg.failover),
            }
        },
        status_code=status_out,
        headers={"x-trace-id": request_id},
    )


async def _non_stream_response(
    *, resp: httpx.Response, request_id: str, trace: Any, store: Store,
    cfg: Config, upstream: Upstream, model: str | None, requested_model: str,
    client_key_name: str, ts: int, latency_ms: float, path: str, status: int,
    req_body: bytes = b"",
) -> Response:
    trace.start("parse", status=status, bytes=len(resp.content))
    payload: dict[str, Any] = {}
    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError):
        payload = {}

    # 上游可能返回数组型 content（同流式接口），压平后再回给客户端
    normalized = (
        status < 400
        and _is_openai_chat_path(path)
        and _normalize_openai_payload(payload)
    )

    if status >= 400:
        log.warning("[%s] upstream HTTP error status=%d", request_id[:8], status)
        trace.add("error", 0, category="upstream_error", stage="response", status=status)

    usage = _extract_usage(payload, path)
    actual_model = payload.get("model") or model
    up_cost = upstream_cost(payload)

    trace.start("record", tokens=usage, upstream_cost=up_cost)
    usage_rec = record_from_usage(
        source=upstream.name, model=actual_model, requested_model=requested_model,
        client_key=client_key_name, usage=usage, ts=ts,
        latency_ms=latency_ms, status=status, request_id=request_id,
        prices=cfg.prices if any(usage.values()) else None, upstream_cost_usd=up_cost,
    )
    trace.end_current()

    trace_summary = trace.finish()
    await asyncio.to_thread(
        store.record_request,
        usage_record=usage_rec,
        trace=trace_summary,
        ts=ts,
        source=upstream.name,
        model=actual_model,
        requested_model=requested_model,
        client_key=client_key_name,
        status=status,
    )
    log.info(
        "[%s] %s/%s %d %s",
        request_id[:8], upstream.name, actual_model, status,
        " ".join(
            f"{s['name']}={s['duration_ms']:.0f}ms"
            for s in trace_summary["spans"] if s.get("duration_ms") is not None
        ),
    )
    # 生成速度：输出 token / 总耗时
    tps = round(usage.get("output", 0) / (latency_ms / 1000), 2) if latency_ms > 0 else 0

    return Response(
        content=(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if normalized else resp.content
        ),
        status_code=status,
        headers={
            **{
                k: v for k, v in resp.headers.items()
                if k.lower() not in {"content-length", "content-encoding", "transfer-encoding"}
            },
            "x-trace-id": request_id,
            "x-upstream": upstream.name,
            "x-tok-per-sec": f"{tps:.2f}",
            "x-latency-ms": f"{latency_ms:.0f}",
            "x-tokens-out": str(usage.get("output", 0)),
        },
        media_type=resp.headers.get("content-type"),
    )


async def _fake_stream_response(
    *, resp: httpx.Response, request_id: str, trace: Any, store: Store,
    cfg: Config, upstream: Upstream, model: str | None, requested_model: str,
    client_key_name: str, ts: int, latency_ms: float, path: str, status: int,
) -> Response:
    """把非流式的完整 JSON 响应包装成 SSE，供只支持非流式的上游使用。"""
    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError):
        payload = {}

    # 数组型 content 先压平，下面取 msg.content 时才拿得到纯文本
    if _is_openai_chat_path(path):
        _normalize_openai_payload(payload)

    usage = _extract_usage(payload, path)
    actual_model = payload.get("model") or model
    up_cost = upstream_cost(payload)

    usage_rec = record_from_usage(
        source=upstream.name, model=actual_model, requested_model=requested_model,
        client_key=client_key_name, usage=usage, ts=ts,
        latency_ms=latency_ms, status=status, request_id=request_id,
        prices=cfg.prices if any(usage.values()) else None, upstream_cost_usd=up_cost,
    )
    trace_summary = trace.finish()
    await asyncio.to_thread(
        store.record_request,
        usage_record=usage_rec,
        trace=trace_summary,
        ts=ts,
        source=upstream.name,
        model=actual_model,
        requested_model=requested_model,
        client_key=client_key_name,
        status=status,
    )

    # 取出完整文本
    content = ""
    reasoning = ""
    tool_calls: list[dict[str, Any]] = []
    finish_reason = "stop"
    choices = payload.get("choices") or []
    if choices and isinstance(choices[0], dict):
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        raw_calls = msg.get("tool_calls") or []
        if isinstance(raw_calls, list):
            tool_calls = [c for c in raw_calls if isinstance(c, dict)]
        upstream_finish = choices[0].get("finish_reason")
        if isinstance(upstream_finish, str) and upstream_finish:
            finish_reason = upstream_finish

    # 有工具调用时，规范成 OpenAI 的 finish_reason
    if tool_calls:
        finish_reason = "tool_calls"

    # 上游返回了 token 却既没有正文也没有工具调用 —— 多半是内容被上游吞了，
    # 这里必须留痕，否则客户端只会看到一条空气回复，无从排查。
    if not content.strip() and not reasoning.strip() and not tool_calls:
        log.warning(
            "[%s] %s/%s 返回空内容 (finish=%s, out=%s tokens) —— "
            "上游异常或触发了安全过滤",
            request_id, upstream.name, actual_model, finish_reason,
            usage.get("output", 0),
        )

    created = payload.get("created") or int(time.time())
    base_id = payload.get("id") or request_id

    def chunk(delta: dict, finish: str | None = None, extra_usage: dict | None = None) -> bytes:
        obj = {
            "id": base_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": actual_model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if extra_usage is not None:
            obj["usage"] = extra_usage
        else:
            obj["usage"] = None
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()

    async def gen():
        yield chunk({"role": "assistant", "content": ""})
        if reasoning:
            # 推理内容先发
            for i in range(0, len(reasoning), 200):
                yield chunk({"reasoning_content": reasoning[i:i + 200]})
        if content:
            for i in range(0, len(content), 200):
                yield chunk({"content": content[i:i + 200]})

        # 工具调用（关键：非流式上游的 tool_calls 必须在这里补发出来，
        # 否则客户端拿不到工具调用，整轮会静默结束）
        for idx, call in enumerate(tool_calls):
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            args = fn.get("arguments")
            if not isinstance(args, str):
                # 少数上游已经把 arguments 解析成对象了
                try:
                    args = json.dumps(args, ensure_ascii=False) if args is not None else ""
                except (TypeError, ValueError):
                    args = ""
            yield chunk({
                "tool_calls": [{
                    "index": idx,
                    "id": call.get("id") or f"call_{request_id[:8]}_{idx}",
                    "type": call.get("type") or "function",
                    "function": {"name": name, "arguments": args},
                }],
            })

        # 最后一帧带 usage
        yield chunk({}, finish=finish_reason, extra_usage=payload.get("usage") or {
            "prompt_tokens": usage.get("input", 0),
            "completion_tokens": usage.get("output", 0),
            "total_tokens": usage.get("input", 0) + usage.get("output", 0),
        })
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        gen(),
        status_code=status,
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache",
            "x-accel-buffering": "no",
            "x-trace-id": request_id,
            "x-upstream": upstream.name,
            "x-fake-stream": "1",
        },
    )


def _stream_response(
    *, resp: httpx.Response, request_id: str, trace: Any, store: Store,
    cfg: Config, upstream: Upstream, model: str | None, requested_model: str,
    client_key_name: str, ts: int, started: float, path: str, body: bytes,
) -> Response:
    collected: dict[str, Any] = {}
    first_token_at: list[float] = []
    text_sink: list[str] = []
    content_type = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    is_sse = content_type == "text/event-stream"
    observe_sse = is_sse or not content_type  # Preserve legacy headerless upstreams.
    terminal: dict[str, Any] = {}

    async def streamer():
        result = "success"
        error_kind = None
        try:
            async def source():
                if observe_sse:
                    async for chunk in _iter_normalized_sse(
                        resp, path, collected=collected, text_sink=text_sink,
                        first_token_at=first_token_at, terminal=terminal,
                    ):
                        yield chunk
                else:
                    async for chunk in resp.aiter_bytes():
                        yield chunk

            stream = source()
            if is_sse:
                stream = with_sse_heartbeat(stream, getattr(cfg, "sse_heartbeat_sec", 0))
            try:
                async for chunk in stream:
                    yield chunk
            finally:
                await stream.aclose()
            if terminal.get("error"):
                result, error_kind = "upstream_error", "sse_error"
            elif terminal.get("unobservable") or not observe_sse:
                result = "unknown"
            elif _is_openai_chat_path(path) or "/messages" in path:
                if not terminal.get("done"):
                    result, error_kind = "stream_interrupted", "missing_terminal"
            else:
                result = "unknown"
        except (asyncio.CancelledError, GeneratorExit):
            result = "client_cancelled"
            error_kind = "client_cancelled"
            raise
        except httpx.HTTPError as exc:
            result = "stream_interrupted"
            error_kind = type(exc).__name__
            # The last bytes may be a non-SSE body or an incomplete SSE event.
            # Never append a synthetic frame to an unknown stream boundary.
        except Exception as exc:
            result = "stream_interrupted"
            error_kind = type(exc).__name__
            raise
        finally:
            try:
                await resp.aclose()
            except Exception:
                pass
            latency = (time.perf_counter() - started) * 1000
            # 优先取第一个携带真实生成内容（正文/思考/tool_calls）的帧作为 TTFT，
            # 排除部分上游提前数秒发送的空 role:assistant 预热帧对首字与解码速度的干扰。
            first_ts = first_token_at[0] if first_token_at else None
            ttft = (first_ts - started) * 1000 if first_ts is not None else None
            usage = collected.get("usage") or {}
            estimated = False

            if not (usage and any(usage.values())):
                # 仅当上游确实没返回 usage 时，才懒加载估算输入与输出 token（含 tool_calls）
                prompt_est = estimate_prompt_tokens(body)
                out_est = estimate_tokens("".join(text_sink))
                if prompt_est or out_est:
                    usage = {
                        "input": prompt_est,
                        "output": out_est,
                        "cache_read": 0,
                        "cache_write": 0,
                        "reasoning": 0,
                    }
                    estimated = True

            actual_model = collected.get("model") or model
            usage_rec = record_from_usage(
                    source=upstream.name,
                    model=actual_model,
                    requested_model=requested_model,
                    client_key=client_key_name,
                    usage=usage,
                    ts=ts,
                    latency_ms=latency,
                    status=resp.status_code,
                    request_id=request_id,
                    prices=cfg.prices if any(usage.values()) else None,
                    upstream_cost_usd=collected.get("cost"),
                    estimated=estimated,
                    ttft_ms=ttft,
                )
            if result not in ("success", "unknown"):
                trace.add("error", 0, category=result, stage="stream", kind=error_kind)
            trace_summary = trace.finish()
            await asyncio.to_thread(
                store.record_request,
                usage_record=usage_rec,
                trace=trace_summary,
                ts=ts,
                source=upstream.name,
                model=actual_model,
                requested_model=requested_model,
                client_key=client_key_name,
                status=resp.status_code,
                result=result,
                error_stage="stream" if result not in ("success", "unknown") else None,
                error_kind=error_kind,
            )
            # 生成速度（tok/s）
            out_tokens = usage.get("output", 0) if usage else 0
            tps = round(out_tokens / (latency / 1000), 2) if latency > 0 else 0
            gen_tps = 0.0
            if ttft is not None and latency > ttft:
                gen_tps = round(out_tokens / ((latency - ttft) / 1000), 2)
            log.info(
                "[%s] %s/%s stream %d ttft=%sms total=%sms out=%d tok/s=%.2f (gen %.2f)",
                request_id[:8], upstream.name, actual_model, resp.status_code,
                f"{ttft:.0f}" if ttft is not None else "?",
                f"{latency:.0f}", out_tokens, tps, gen_tps,
            )

    return StreamingResponse(
        streamer(),
        status_code=resp.status_code,
        media_type="text/event-stream" if observe_sse else (content_type or None),
        headers={
            "cache-control": "no-cache",
            "x-accel-buffering": "no",
            "x-trace-id": request_id,
            "x-upstream": upstream.name,
        },
    )


def _merge_usage_from_sse(
    chunk: bytes, collected: dict[str, Any], path: str, text_sink: list[str] | None = None,
) -> None:
    """从 SSE 分片里尽力解析 usage 与输出文本（保留兼容辅助函数）。"""
    try:
        text = chunk.decode("utf-8", errors="ignore")
    except Exception:
        return

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            _ingest_sse_obj(obj, path, collected, text_sink, None)
