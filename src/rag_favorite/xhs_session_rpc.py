"""Bounded private RPC to the sole owner of the Xiaohongshu profile."""

from __future__ import annotations

import json
import os
import socket
import stat
import time

from .video_provider import ProviderUnavailable
from .xhs_session_policy import failure

MAX_RESPONSE = 12_000_000


class SessionRPC:
    def __init__(self, video):
        self.root = video.root.parent / "xhs-session"

    def call(self, operation, *, wait=False, **params):
        deadline = time.monotonic() + 125
        while True:
            try:
                return self._call(operation, params)
            except ProviderUnavailable as exc:
                delay = exc.diagnostics.get("retry_after", 0)
                if (
                    wait
                    and exc.code in {"XHS_DEFERRED", "XHS_SESSION_BUSY"}
                    and 0 < delay <= 61
                    and time.monotonic() + delay < deadline
                ):
                    time.sleep(delay)
                    continue
                raise

    def _call(self, operation, params):
        path = self.root / "session.sock"
        try:
            metadata = path.lstat()
            if (
                not stat.S_ISSOCK(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077
            ):
                raise OSError()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(7200 if operation == "download_media" else 190)
                sock.connect(str(path))
                payload = (
                    json.dumps({"operation": operation, "params": params}).encode()
                    + b"\n"
                )
                if len(payload) > 65536:
                    raise ValueError("XHS_RPC_INPUT_INVALID")
                sock.sendall(payload)
                chunks, size = [], 0
                while True:
                    block = sock.recv(65536)
                    if not block:
                        break
                    size += len(block)
                    if size > MAX_RESPONSE:
                        raise OSError()
                    chunks.append(block)
                result = json.loads(b"".join(chunks))
        except (OSError, ValueError):
            raise failure("XHS_SESSION_UNAVAILABLE", delay=30) from None
        if "error" in result:
            error = result["error"]
            raise ProviderUnavailable(
                error["code"],
                code=error["code"],
                diagnostics=error.get("diagnostics", {}),
                status_code=error.get("status_code"),
                retryable=error.get("retryable", False),
            )
        return result["result"]
