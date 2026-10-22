"""Opt-in, deadline-bounded timing events; never change ingestion outcomes."""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from uuid import uuid4

_context = ContextVar("rag_observation_job", default=None)
_parent = ContextVar("rag_observation_span", default=None)
_controls = {}


def _destination(video):
    now = time.time()
    key = str(video.root)
    cached = _controls.get(key)
    if cached is None or now - cached[0] >= 1:
        try:
            control = json.loads((video.root / "observation-control.json").read_text())
            run_id = control["run_id"]
            if (
                control.get("enabled") is not True
                or not re.fullmatch(r"[a-z0-9-]{1,80}", run_id)
                or not isinstance(control["ends_at_epoch"], (int, float))
            ):
                raise ValueError("Invalid observation control")
            result = (video.root / "observations" / run_id, control["ends_at_epoch"])
        except (OSError, ValueError, TypeError, KeyError):
            result = None
        cached = _controls[key] = (now, result)
    result = cached[1]
    return result[0] if result and now < result[1] else None


def emit(video, job_id, event, **fields):
    """Append one small metadata event using one O_APPEND write."""
    try:
        destination = _destination(video)
        if destination is None:
            return
        # The recorder creates the directory before enabling the control file.
        value = {
            "version": 1,
            "at": datetime.now(UTC).isoformat(),
            "monotonic_ns": time.monotonic_ns(),
            "pid": os.getpid(),
            "job_id": str(job_id),
            "event": event,
            "parent_span_id": _parent.get(),
            **fields,
        }
        data = (json.dumps(value, ensure_ascii=False, default=str) + "\n").encode()
        fd = os.open(
            destination / "events.jsonl", os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
        )
        try:
            if os.write(fd, data) != len(data):
                raise OSError("Partial observation write")
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError):
        # Report an observable loss counter without exposing payloads/errors.
        try:
            path = video.root / f"observation-errors-{os.getpid()}.jsonl"
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(fd, b'{"event":"observation_write_failed"}\n')
            finally:
                os.close(fd)
        except OSError:
            pass


@contextmanager
def observation_job(video, job_id):
    token = _context.set((video, str(job_id)))
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def span(kind, operation, **metadata):
    context = _context.get()
    if context is None or _destination(context[0]) is None:
        yield
        return
    identifier = uuid4().hex
    parent = _parent.get()
    fields = {
        "span_id": identifier,
        "parent_span_id": parent,
        "kind": kind,
        "operation": operation,
        **metadata,
    }
    started = time.monotonic()
    emit(*context, "span_start", **fields)
    token = _parent.set(identifier)
    outcome, error = "success", None
    try:
        yield
    except BaseException as exc:
        outcome = "failure" if isinstance(exc, Exception) else "interrupted"
        candidate = getattr(exc, "code", None)
        error = (
            candidate
            if isinstance(candidate, str)
            and re.fullmatch(r"[A-Z0-9_]{1,100}", candidate)
            else type(exc).__name__
        )
        raise
    finally:
        emit(
            *context,
            "span_end",
            **fields,
            outcome=outcome,
            error_code=error,
            duration_seconds=time.monotonic() - started,
        )
        _parent.reset(token)


def timed(operation, *, kind="operation"):
    """Time a selected boundary; never serialize arguments or return values."""

    def decorate(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            with span(kind, operation):
                return function(*args, **kwargs)

        return wrapper

    return decorate
