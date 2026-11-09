"""Privacy-preserving gateway session attribution, independent of request storage.

Integration: construct ``SessionAttributor(data_dir)`` once, strip
``X-Session-ID`` from outbound headers, and call ``fingerprint`` with the
single inbound header value and stable, authenticated client and source names.
Persist only the returned digest (or NULL), never the header. Reject duplicate
header instances at the HTTP boundary. Invalid IDs raise ValueError; missing IDs
return None and must not be inferred from historical records.

Keep the key file with the database across restarts/backups. Deleting or
rotating it breaks correlation with existing fingerprints. Protect the data
directory with OS access controls, particularly on Windows where POSIX mode
bits do not enforce private ACLs.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
import tempfile
from pathlib import Path

SESSION_HEADER = "X-Session-ID"
KEY_FILENAME = "session-attribution.key"
_KEY_SIZE = 32
_ID_PATTERN = re.compile(r"[A-Za-z0-9._~-]{1,128}\Z", re.ASCII)
_DOMAIN = b"usage-gateway/session-attribution/v1\x00"


def validate_session_id(value: str) -> None:
    """Accept 1–128 ASCII URL-safe unreserved characters; do not normalize."""
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid X-Session-ID")


def _read_key(path: Path) -> bytes:
    # O_NOFOLLOW prevents following a substituted symlink where supported.
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("session attribution key must be a regular file")
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("session attribution key changed while opening")
        if os.name == "posix" and info.st_mode & 0o077:
            raise PermissionError("session attribution key must not be accessible to group or others")
        with os.fdopen(fd, "rb", closefd=False) as f:
            key = f.read(_KEY_SIZE + 1)
        if len(key) != _KEY_SIZE:
            raise ValueError("invalid session attribution key length")
        return key
    finally:
        os.close(fd)


def _load_or_create_key(data_dir: Path) -> bytes:
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = data_dir / KEY_FILENAME
    try:
        return _read_key(path)
    except FileNotFoundError:
        pass

    # Publish a fully written key atomically. A competing process wins via
    # link(), never by overwriting an existing key or exposing a partial one.
    fd, name = tempfile.mkstemp(prefix=".session-key-", dir=data_dir)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(_KEY_SIZE))
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        except OSError:
            # FAT32/exFAT volumes do not support hard links. rename() in the
            # same directory still publishes a complete file and, on Windows,
            # refuses to replace a key created by another process.
            try:
                os.rename(temporary, path)
            except FileExistsError:
                pass
        return _read_key(path)
    finally:
        temporary.unlink(missing_ok=True)


def _field(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("client identity and source must be nonempty strings")
    encoded = value.encode("utf-8")
    if len(encoded) > 0xFFFFFFFF:
        raise ValueError("attribution field too long")
    return len(encoded).to_bytes(4, "big") + encoded


class SessionAttributor:
    """Derive a stable opaque digest scoped to a client and source."""

    def __init__(self, data_dir: str | Path):
        self._key = _load_or_create_key(Path(data_dir))

    def fingerprint(self, session_id: str | None, *, client_id: str, source: str) -> str | None:
        if session_id is None:
            return None
        validate_session_id(session_id)
        message = _DOMAIN + _field(client_id) + _field(source) + _field(session_id)
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()
