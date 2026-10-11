"""Best-effort, credential-free activity records for the current video job."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from uuid import uuid4

KEYS = {"CCR", "ASR", "Embedding", "ImageBind"}
_job = ContextVar("rag_model_activity_job", default=None)
_call = ContextVar("rag_model_activity_call", default=None)


def read_calls(video):
    """Return recent individual calls; legacy snapshots have no call history."""
    try:
        path = video.root / "model-activity.json"
        if path.stat().st_size > 2_000_000:
            return {}
        raw = json.loads(path.read_text())
        calls = raw.get("calls", {})
        if raw.get("version") != 1 or not isinstance(calls, dict):
            return {}
        return {
            key: value
            for key, value in calls.items()
            if isinstance(value, dict)
            and value.get("model") in KEYS
            and value.get("status")
            in {"running", "success", "failure", "interrupted", "cached"}
            and isinstance(value.get("job_id"), str)
            and type(value.get("pid")) is int
            and value.get("call_id") == key
            and isinstance(value.get("updated_at"), str)
            and (
                value.get("status") != "running"
                or isinstance(value.get("started_at"), str)
            )
        }
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def read_activity(video):
    try:
        path = video.root / "model-activity.json"
        if path.stat().st_size > 2_000_000:
            return {}
        raw = json.loads(path.read_text())
        if raw.get("version") != 1 or not isinstance(raw.get("models"), dict):
            return {}
        models = {}
        for key, value in raw["models"].items():
            if key not in KEYS or not isinstance(value, dict):
                continue
            if value.get("status") not in {
                "running",
                "success",
                "failure",
                "interrupted",
                "cached",
            }:
                continue
            if type(value.get("pid")) is not int or not isinstance(
                value.get("job_id"), str
            ):
                continue
            if any(
                type(value.get(field, 0)) is not int or value.get(field, 0) < 0
                for field in ("calls", "completed_calls", "cache_hits")
            ):
                continue
            models[key] = value
        return models
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _write(video, key, event, call_id, operation, duration=None, metadata=None):
    if key not in KEYS:
        return
    context = _job.get()
    if context is None:
        return
    try:
        video.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (video.root / "model-activity.lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            deadline = time.monotonic() + 0.2
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        return
                    time.sleep(0.01)
            models = read_activity(video)
            calls = read_calls(video)
            old = models.get(key, {})
            entry = dict(old)
            stamp = datetime.now(UTC).isoformat()
            if event == "diagnostics":
                if call_id not in calls:
                    return
            elif event in {"running", "cached"}:
                entry.update(
                    pid=os.getpid(),
                    job_id=context[1],
                    operation=operation,
                    call_id=call_id,
                    status=event,
                    updated_at=stamp,
                )
                if event == "running":
                    entry.update(started_at=stamp, calls=old.get("calls", 0) + 1)
                else:
                    entry["cache_hits"] = old.get("cache_hits", 0) + 1
                    entry["last_cache_at"] = stamp
            elif call_id in calls or old.get("call_id") == call_id:
                entry.update(
                    status=event,
                    updated_at=stamp,
                    last_completed_at=stamp,
                    last_duration_seconds=round(duration or 0, 4),
                    last_outcome=event,
                    completed_calls=old.get("completed_calls", 0) + 1,
                )
            else:
                return
            call = dict(calls.get(call_id, {}))
            call.update(
                model=key,
                pid=os.getpid(),
                job_id=context[1],
                operation=operation,
                call_id=call_id,
                updated_at=stamp,
            )
            if event != "diagnostics":
                call["status"] = event
            if metadata:
                call.update(metadata)
            if event == "running":
                call["started_at"] = stamp
            elif event not in {"cached", "diagnostics"}:
                call.update(
                    completed_at=stamp, duration_seconds=round(duration or 0, 4)
                )
            calls[call_id] = call
            active = [
                c
                for c in calls.values()
                if c["model"] == key and c["status"] == "running"
            ]
            if active:
                # A short overlapping call must not hide an older active call.
                current = max(active, key=lambda c: c["started_at"])
                entry.update(
                    {
                        field: current[field]
                        for field in (
                            "pid",
                            "job_id",
                            "operation",
                            "call_id",
                            "status",
                            "updated_at",
                            "started_at",
                        )
                    }
                )
            finished = sorted(
                (c for c in calls.values() if c["status"] != "running"),
                key=lambda c: c["updated_at"],
                reverse=True,
            )
            keep = {c["call_id"] for c in finished[:256]}
            calls = {
                k: v for k, v in calls.items() if v["status"] == "running" or k in keep
            }
            models[key] = entry
            fd, name = tempfile.mkstemp(prefix=".model-activity-", dir=video.root)
            try:
                with os.fdopen(fd, "w") as output:
                    json.dump({"version": 1, "models": models, "calls": calls}, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(name, video.root / "model-activity.json")
            finally:
                if os.path.exists(name):
                    os.unlink(name)
    except (OSError, ValueError, TypeError):
        pass  # Monitoring must never fail a model call or change job semantics.


def annotate_call(**metadata):
    """Add only credential-free outcome metrics to the current individual call."""
    context, current = _job.get(), _call.get()
    if context is None or current is None:
        return
    allowed = {
        "http_status",
        "stream_complete",
        "valid_output",
        "request_bytes",
        "image_bytes",
        "images",
        "error_category",
        "retryable",
        "request_id",
    }
    _write(
        context[0],
        current[0],
        "diagnostics",
        current[1],
        current[2],
        metadata={k: v for k, v in metadata.items() if k in allowed},
    )


@contextmanager
def job_activity(video, job_id):
    token = _job.set((video, str(job_id)))
    try:
        yield
    finally:
        _job.reset(token)


@contextmanager
def activity_call(key, operation):
    context = _job.get()
    if context is None or key not in KEYS:
        yield
        return
    video, _ = context
    call_id = uuid4().hex
    token = _call.set((key, call_id, operation))
    _write(video, key, "running", call_id, operation)
    start, status = time.monotonic(), "success"
    try:
        yield
    except BaseException as exc:
        status = "failure" if isinstance(exc, Exception) else "interrupted"
        raise
    finally:
        _write(video, key, status, call_id, operation, time.monotonic() - start)
        _call.reset(token)


def model_call(key, operation):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            with activity_call(key, operation):
                return function(*args, **kwargs)

        return call

    return decorate


def cache_hit(key, operation):
    context = _job.get()
    if context is not None:
        _write(context[0], key, "cached", uuid4().hex, operation)
