from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType
from typing import Self

from .state import ensure_private_directory


class SessionAlreadyRunning(RuntimeError):
    pass


class SessionLock:
    def __init__(self, path: Path):
        self.path = path
        self._descriptor: int | None = None

    def acquire(self) -> None:
        ensure_private_directory(self.path.parent)
        descriptor = os.open(
            self.path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise SessionAlreadyRunning(
                "Xiaohongshu session manager is already running"
            ) from exc
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()}\n".encode())
        os.fsync(descriptor)
        self._descriptor = descriptor

    def release(self) -> None:
        if self._descriptor is None:
            return
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self._descriptor)
            self._descriptor = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def cleanup_stale_lock(path: Path) -> bool:
    lock = SessionLock(path)
    try:
        lock.acquire()
    except SessionAlreadyRunning:
        return False
    try:
        if lock._descriptor is not None:
            os.ftruncate(lock._descriptor, 0)
            os.fsync(lock._descriptor)
        return True
    finally:
        lock.release()
