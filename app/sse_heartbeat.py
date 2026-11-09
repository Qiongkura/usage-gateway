"""Optional SSE keepalives without changing upstream chunks or their semantics.

The caller must use this only for an SSE response, and must not count the
comment as a model token. Each wrapper owns its upstream iterator and closes it
on normal completion, errors, cancellation, or explicit generator close.
"""
from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import suppress


class _Boundary:
    """Track only framing, without buffering potentially unbounded event data."""

    def __init__(self) -> None:
        self.safe = True
        self._line_empty = True
        self._cr = False

    def _end_line(self) -> None:
        self.safe = self._line_empty
        self._line_empty = True

    def feed(self, chunk: bytes) -> None:
        for byte in chunk:
            if self._cr:
                self._cr = False
                self._end_line()
                if byte == 10:
                    continue
            # Do not insert between CR and a possible LF, or inside a block.
            self.safe = False
            if byte == 13:
                self._cr = True
            elif byte == 10:
                self._end_line()
            else:
                self._line_empty = False


async def with_sse_heartbeat(
    upstream: AsyncIterable[bytes],
    interval: float | None = None,
    *,
    heartbeat: bytes = b": keep-alive\n\n",
) -> AsyncIterator[bytes]:
    """Yield upstream chunks unchanged and periodic comments while awaiting one.

    ``None`` or zero disables heartbeats. Positive intervals are seconds since
    the last yielded chunk or heartbeat, while at an SSE block boundary.
    No heartbeat is inserted into a partial block or between CR and LF, so an
    incomplete upstream block can delay heartbeats indefinitely. If a heartbeat
    precedes upstream bytes, a leading BOM is moved before the comment so an
    upstream BOM (even split across chunks) retains its SSE semantics. ``heartbeat``
    must be a complete SSE comment block. The consumer should explicitly call ``aclose()`` when it
    stops early; cancellation of an active iteration also triggers cleanup.

    A pending upstream __anext__ is never cancelled just to send a heartbeat.
    Like any streaming wrapper, a caller that abandons an unclosed iterator
    without cancellation cannot rely on prompt cleanup.
    """
    if interval is not None and (not math.isfinite(interval) or interval < 0):
        raise ValueError("interval must be a finite non-negative number or None")
    # Restrict custom heartbeats to comment-only complete blocks, so they can
    # never be mistaken for data events by an SSE client.
    if not isinstance(heartbeat, bytes) or not heartbeat.endswith(b"\n\n"):
        raise ValueError("heartbeat must be a complete LF-terminated SSE comment block")
    if any(not line.startswith(b":") or b"\r" in line
           for line in heartbeat[:-2].split(b"\n")):
        raise ValueError("heartbeat must contain only SSE comment lines")

    iterator = aiter(upstream)
    pending: asyncio.Future[bytes] | None = None
    boundary = _Boundary()
    leading_heartbeat = False
    prefix = bytearray()
    prefix_done = False
    bom = b"\xef\xbb\xbf"
    try:
        while True:
            # ensure_future accepts arbitrary awaitables returned by __anext__.
            pending = asyncio.ensure_future(anext(iterator))
            while True:
                done, _ = await asyncio.wait(
                    {pending}, timeout=interval if interval and boundary.safe else None
                )
                if done:
                    try:
                        chunk = pending.result()
                    except StopAsyncIteration:
                        pending = None
                        if prefix:
                            yield bytes(prefix)
                        return
                    pending = None
                    if leading_heartbeat and not prefix_done:
                        prefix.extend(chunk)
                        if len(prefix) < len(bom) and bom.startswith(prefix):
                            break
                        chunk = bytes(prefix)
                        prefix.clear()
                        if chunk.startswith(bom):
                            chunk = chunk[len(bom):]
                        prefix_done = True
                    if chunk:
                        if not leading_heartbeat:
                            prefix_done = True
                        boundary.feed(chunk)
                        yield chunk
                    break
                if boundary.safe:
                    if not prefix_done and not leading_heartbeat:
                        leading_heartbeat = True
                        yield bom + heartbeat
                    else:
                        yield heartbeat
    finally:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            # Retrieve failures too, to avoid unobserved-task warnings.
            with suppress(asyncio.CancelledError, Exception):
                await pending
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()
