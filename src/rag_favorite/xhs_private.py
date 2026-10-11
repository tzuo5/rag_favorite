"""Owner-only session files and bounded HTTPS redirects for source ingestion."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .config import ConfigError


def private_directory(path: Path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise ConfigError("Unsafe private runtime directory.")
    path.chmod(0o700)


def private_open(path: Path, *, append=False):
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    flags |= os.O_APPEND if append else os.O_EXCL
    fd = os.open(path, flags, 0o600)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
    ):
        os.close(fd)
        raise ConfigError("Unsafe private runtime file.")
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "a" if append else "w", encoding="utf-8")


def private_json(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "r", encoding="utf-8") as f:
        info = os.fstat(f.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 2_000_000
        ):
            raise ConfigError("Unsafe private state file.")
        value = json.load(f)
    if not isinstance(value, dict):
        raise ConfigError("Invalid private state.")
    return value


def atomic_private_json(path: Path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def canonical_note_url(url):
    part = urlsplit(url)
    return urlunsplit((part.scheme, part.netloc, part.path, "", ""))


class TrustedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed):
        self.allowed = allowed
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        part = urlsplit(newurl)
        if (
            part.scheme != "https"
            or part.username
            or part.password
            or part.port not in {None, 443}
            or not self.allowed(part.hostname or "")
        ):
            raise ConfigError("Source redirect left its trusted HTTPS origin.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@contextmanager
def browser_session_lock(session, *, timeout=10):
    from .video_provider import ProviderUnavailable

    private_directory(session)
    with private_open(session / "browser.lock", append=True) as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ProviderUnavailable(
                        "XHS_SESSION_BUSY", code="XHS_SESSION_BUSY"
                    ) from None
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
