"""Build a minimal, offline-only diagnostic bundle from untrusted trace data.

This module does not fetch traces, write files, contact a network, or replay requests.
Pass plain decoded JSON dictionaries (or dictionaries made from database rows) to
``build_diagnostic_bundle``. The result is a new JSON-serializable dictionary.
Only explicitly selected, validated fields are copied; identifiers, arbitrary
strings, errors and endpoint details are deliberately excluded. Even an allowed
field is never assumed safe merely because of its name.

Trace and span counts and serialized span input are capped. Invalid values are
omitted, and malformed spans_json is treated as an empty span list. The caller
is responsible for authorization and for securing any exported bundle; timing,
status and usage metadata can itself be sensitive.
"""
from __future__ import annotations

import json
import math
from typing import Any

MAX_TRACES = 500
MAX_SPANS = 100
MAX_SPANS_JSON_BYTES = 1_000_000

_SPAN_NAMES = frozenset({"receive", "route", "skip", "upstream", "give_up", "parse", "record"})
_TOKEN_FIELDS = frozenset({"input", "output", "cache_read", "cache_creation"})


def _number(value: Any, maximum: int, *, integer: bool = False) -> int | float | None:
    # Exact types avoid bool-as-int and user-defined numeric operators.
    if type(value) not in (int, float):
        return None
    if type(value) is float and not math.isfinite(value):
        return None
    if value < 0 or value > maximum or (integer and type(value) is not int):
        return None
    return value


def _copy_number(source: dict, target: dict, key: str, maximum: int, *, integer: bool = False) -> None:
    value = _number(source.get(key), maximum, integer=integer)
    if value is not None:
        target[key] = value


def _span(value: Any) -> dict | None:
    if type(value) is not dict:
        return None
    result: dict[str, Any] = {}
    name = value.get("name")
    if type(name) is str and name in _SPAN_NAMES:
        result["name"] = name
    for key in ("start_ms", "end_ms", "duration_ms"):
        _copy_number(value, result, key, 604_800_000)
    # Never include the error message; indicate only whether a string was present.
    error = value.get("error")
    if type(error) is str:
        result["has_error"] = bool(error)
    detail = value.get("detail")
    if type(detail) is dict:
        safe_detail: dict[str, Any] = {}
        for key, maximum in (("status", 599), ("bytes", 1_000_000_000), ("attempt", 10_000)):
            _copy_number(detail, safe_detail, key, maximum, integer=True)
        tokens = detail.get("tokens")
        if type(tokens) is dict:
            safe_tokens: dict[str, Any] = {}
            for key in sorted(_TOKEN_FIELDS):
                _copy_number(tokens, safe_tokens, key, 1_000_000_000, integer=True)
            if safe_tokens:
                safe_detail["tokens"] = safe_tokens
        _copy_number(detail, safe_detail, "upstream_cost", 1_000_000_000)
        if safe_detail:
            result["detail"] = safe_detail
    return result


def _spans(trace: dict) -> list:
    spans = trace.get("spans")
    if type(spans) is not list:
        raw = trace.get("spans_json")
        # Checking characters before encoding bounds the allocation; UTF-8 bytes
        # are checked separately, since characters can occupy multiple bytes.
        if type(raw) is not str or len(raw) > MAX_SPANS_JSON_BYTES:
            return []
        try:
            if len(raw.encode("utf-8")) > MAX_SPANS_JSON_BYTES:
                return []
            spans = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            return []
    if type(spans) is not list:
        return []
    result = []
    for item in spans[:MAX_SPANS]:
        span = _span(item)
        if span is not None:
            result.append(span)
    return result


def build_diagnostic_bundle(traces: list) -> dict:
    """Return a versioned, redacted bundle for up to 500 plain trace dicts.

    ``traces`` must be a plain list; other root types raise ValueError. Non-dict
    entries are ignored. Each trace may use ``spans`` or database ``spans_json``.
    A list in ``spans`` takes precedence, including an empty list. There is no
    opt-in for raw content and no replay capability.
    """
    if type(traces) is not list:
        raise ValueError("traces must be a list")
    safe_traces = []
    for trace in traces[:MAX_TRACES]:
        if type(trace) is not dict:
            continue
        safe_trace: dict[str, Any] = {}
        _copy_number(trace, safe_trace, "ts", 253_402_300_799, integer=True)
        _copy_number(trace, safe_trace, "status", 599, integer=True)
        _copy_number(trace, safe_trace, "total_ms", 604_800_000)
        safe_trace["spans"] = _spans(trace)
        safe_traces.append(safe_trace)
    return {"schema_version": 1, "traces": safe_traces}
