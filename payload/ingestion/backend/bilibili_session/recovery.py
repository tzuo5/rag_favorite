from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable

from backend.ingestion.config import Settings
from backend.ingestion.discovery.errors import (
    ClassifiedPlatformError,
    classify_platform_error,
)
from backend.ingestion.models import PlatformErrorCode

logger = logging.getLogger(__name__)


def classify_session_error(
    settings: Settings,
    error: BaseException,
) -> ClassifiedPlatformError:
    """Promote Bilibili 412 to expired auth only when a real probe agrees."""
    classified = classify_platform_error(error)
    if (
        not settings.bilibili_session_enabled
        or classified.code != PlatformErrorCode.PLATFORM_BLOCKED
        or not re.search(r"\b(?:http error )?412\b", str(error), re.IGNORECASE)
    ):
        return classified
    try:
        from .cli import SessionManager

        status = SessionManager(settings).status()
    except Exception:
        logger.exception("Bilibili session probe failed during error recovery")
        return classified
    if status.get("session_status") != "AUTH_EXPIRED":
        return classified
    return ClassifiedPlatformError(
        PlatformErrorCode.AUTH_EXPIRED,
        "哔哩哔哩登录状态已失效",
        immediate_open=True,
    )


def trigger_session_refresh(
    settings: Settings,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    if not settings.bilibili_session_enabled:
        return False
    command = [
        "systemctl",
        "--user",
        "start",
        "--no-block",
        "bilibili-session-manager.service",
    ]
    if sys.platform == "darwin":
        command = [
            "launchctl",
            "kickstart",
            "-k",
            f"gui/{os.getuid()}/com.rag-favorite.bilibili-session-manager",
        ]
    try:
        result = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        logger.exception("could not trigger Bilibili session refresh")
        return False
    return result.returncode == 0
