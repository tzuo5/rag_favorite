"""Durable, library-local queue gates shared by the desktop GUI and worker."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from uuid import UUID

from .config import ConfigError


@dataclass(frozen=True)
class QueueControl:
    version: int = 1
    paused: bool = False
    paused_jobs: tuple[str, ...] = ()
    updated_at: str = ""


def read_control(video) -> QueueControl:
    path = video.root / "queue-control.json"
    if not path.exists():
        return QueueControl()
    try:
        if path.stat().st_size > 1_000_000:
            raise ValueError("oversized control file")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(raw, dict)
            or raw.get("version") != 1
            or type(raw.get("paused")) is not bool
            or not isinstance(raw.get("paused_jobs"), list)
        ):
            raise ValueError("invalid queue control")
        ids = tuple(sorted({str(UUID(value)) for value in raw["paused_jobs"]}))
        return QueueControl(1, raw["paused"], ids, str(raw.get("updated_at", "")))
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ConfigError(
            "Invalid queue-control.json; the queue stays paused."
        ) from exc


@contextmanager
def locked_control(video, *, timeout=None):
    """Serialize a Stop acknowledgement with the worker's claim transaction."""
    video.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (video.root / "queue-control.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        if timeout is None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ConfigError(
                            "队列控制正在被 worker 使用，本次操作未完成，请稍后重试。"
                        ) from None
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        try:
            yield read_control(video)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _write_control(video, control):
    fd, name = tempfile.mkstemp(prefix=".queue-control-", dir=video.root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(asdict(control), file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, video.root / "queue-control.json")
        directory = os.open(video.root, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def set_control(
    video, *, paused: bool | None = None, job_id=None, job_paused=None, lock_timeout=3
):
    if paused is not None and type(paused) is not bool:
        raise ValueError("paused must be a boolean")
    if job_id is not None:
        job_id = str(UUID(job_id))
        if type(job_paused) is not bool:
            raise ValueError("job_paused must be a boolean")
    with locked_control(video, timeout=lock_timeout) as previous:
        jobs = set(previous.paused_jobs)
        if job_id is not None:
            if job_paused:
                jobs.add(job_id)
            else:
                jobs.discard(job_id)
        current = replace(
            previous,
            paused=previous.paused if paused is None else paused,
            paused_jobs=tuple(sorted(jobs)),
            updated_at=datetime.now(UTC).isoformat(),
        )
        _write_control(video, current)
        return current
