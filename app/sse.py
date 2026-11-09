"""Incremental, protocol-neutral SSE observation; never reserialize for passthrough.

Feed the exact upstream bytes to this parser *in parallel* with forwarding those
same bytes. Returned blocks are for observation only, including comment-only
blocks and an undispatched EOF tail. On SSELimitError stop observing, not
forwarding: the offending input has already been handed to the caller and the
parser must not be reused. No network, JSON, or provider-specific logic lives
here.
"""
from __future__ import annotations

from dataclasses import dataclass


class SSELimitError(ValueError):
    """A single, not-yet-delimited block exceeded its byte limit."""


@dataclass(frozen=True)
class SSEBlock:
    """A byte-exact block ending in a blank line, or an incomplete EOF tail.

    ``dispatch`` is true only for a blank-line-terminated block with at least
    one data field (including an empty data field). ``data`` joins data fields
    with LF. ``event`` defaults to 'message'; ``last_event_id`` and ``retry``
    reflect the stream state after this block. At EOF, unfinished events are
    discarded by SSE, so their data is deliberately not dispatched.
    """

    raw: bytes
    dispatch: bool
    data: str
    event: str
    last_event_id: str
    retry: int | None


class SSEParser:
    """Parse SSE byte chunks. Limit applies to raw bytes per block, delimiters included.

    UTF-8 is decoded per complete line with replacement for malformed bytes;
    this preserves multibyte characters split across arbitrary input chunks.
    A leading UTF-8 BOM is ignored for parsing, but remains in ``raw``.
    ``feed`` and ``finish`` return lists of SSEBlock. After finish or a size
    error the parser cannot be reused. The default maximum is 1 MiB.
    """

    def __init__(self, max_block_bytes: int = 1024 * 1024):
        if not isinstance(max_block_bytes, int) or isinstance(max_block_bytes, bool) or max_block_bytes < 1:
            raise ValueError("max_block_bytes must be a positive integer")
        self.max_block_bytes = max_block_bytes
        self._raw = bytearray()
        self._line = bytearray()
        self._pending_cr = False
        self._first_line = True
        self._data: list[str] = []
        self._event = ""
        self._last_event_id = ""
        self._retry: int | None = None
        self._closed = False

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("SSE parser is closed")

    def _append(self, byte: int) -> None:
        if len(self._raw) >= self.max_block_bytes:
            self._closed = True
            raise SSELimitError(f"SSE block exceeds {self.max_block_bytes} bytes")
        self._raw.append(byte)

    def _block(self, terminated: bool) -> SSEBlock:
        result = SSEBlock(bytes(self._raw), terminated and bool(self._data),
                          "\n".join(self._data), self._event or "message",
                          self._last_event_id, self._retry)
        self._raw.clear()
        self._data.clear()
        self._event = ""
        return result

    def _end_line(self) -> SSEBlock | None:
        line = bytes(self._line).decode("utf-8", errors="replace")
        self._line.clear()
        if self._first_line:
            self._first_line = False
            line = line.removeprefix("\ufeff")
        if not line:
            return self._block(True)
        if line.startswith(":"):
            return None
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data.append(value)
        elif field == "event":
            self._event = value
        elif field == "id" and "\x00" not in value:
            self._last_event_id = value
        elif field == "retry" and value and all("0" <= c <= "9" for c in value):
            # Python limits conversion of huge integers; SSE retry is advisory.
            # Saturating makes handling malicious digit strings bounded.
            digits = value.lstrip("0") or "0"
            self._retry = int(digits) if len(digits) <= 9 else 999_999_999
        return None

    def feed(self, chunk: bytes) -> list[SSEBlock]:
        self._check_open()
        if not isinstance(chunk, bytes):
            raise TypeError("chunk must be bytes")
        blocks: list[SSEBlock] = []
        for byte in chunk:
            if self._pending_cr:
                self._pending_cr = False
                if byte == 10:
                    self._append(byte)
                    block = self._end_line()
                    if block is not None:
                        blocks.append(block)
                    continue
                block = self._end_line()
                if block is not None:
                    blocks.append(block)
            self._append(byte)
            if byte == 13:
                self._pending_cr = True
            elif byte == 10:
                block = self._end_line()
                if block is not None:
                    blocks.append(block)
            else:
                self._line.append(byte)
        return blocks

    def finish(self) -> list[SSEBlock]:
        """Finish at EOF. An unterminated event is returned but not dispatched."""
        self._check_open()
        self._closed = True
        blocks: list[SSEBlock] = []
        if self._pending_cr:
            self._pending_cr = False
            block = self._end_line()
            if block is not None:
                blocks.append(block)
        if self._raw:
            # EOF is not a line terminator: ignore any unfinished line/fields.
            blocks.append(self._block(False))
        return blocks
