from __future__ import annotations

import json
import os
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .login_probe import SessionStatus

STATE_VERSION = 1
_STATE_KEYS = {
    "version",
    "file_status",
    "session_status",
    "last_probe_at",
    "last_success_at",
    "manual_login_required",
}


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_status() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "file_status": "missing",
        "session_status": SessionStatus.UNKNOWN.value,
        "last_probe_at": None,
        "last_success_at": None,
        "manual_login_required": False,
    }


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise OSError("session directory is not a regular directory")
    path.chmod(0o700)


def _atomic_secure_write(path: Path, payload: bytes) -> None:
    ensure_private_directory(path.parent)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def write_status(path: Path, status: dict[str, Any]) -> dict[str, Any]:
    safe = default_status()
    safe.update({key: status.get(key) for key in _STATE_KEYS if key in status})
    safe["version"] = STATE_VERSION
    safe["session_status"] = SessionStatus(safe["session_status"]).value
    safe["manual_login_required"] = bool(safe["manual_login_required"])
    _atomic_secure_write(
        path,
        (json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n").encode(),
    )
    return safe


def read_status(path: Path) -> dict[str, Any]:
    fallback = default_status()
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            return fallback
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    if not isinstance(raw, dict) or set(raw) - _STATE_KEYS:
        return fallback
    try:
        status = SessionStatus(str(raw.get("session_status")))
    except ValueError:
        return fallback
    result = default_status()
    result.update({key: raw.get(key) for key in _STATE_KEYS if key in raw})
    result["session_status"] = status.value
    result["manual_login_required"] = bool(result["manual_login_required"])
    return result


def status_is_fresh_and_valid(
    status: dict[str, Any],
    *,
    max_age_seconds: int,
    now: datetime | None = None,
) -> bool:
    if status.get("session_status") != SessionStatus.SESSION_VALID.value:
        return False
    value = status.get("last_probe_at")
    if not isinstance(value, str):
        return False
    try:
        probed_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if probed_at.tzinfo is None:
        return False
    current = now or datetime.now(timezone.utc)
    age = (current - probed_at.astimezone(timezone.utc)).total_seconds()
    return 0 <= age <= max_age_seconds
