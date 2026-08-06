from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Callable

from backend.ingestion.config import Settings

logger = logging.getLogger(__name__)


def probe_session_now(
    settings: Settings,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    """Run one bounded real probe for a batch-confirmation decision."""
    if not settings.xiaohongshu_session_enabled:
        return False
    python = settings.root / ".venv/bin/python"
    command = [str(python), "-m", "backend.xhs_session.cli", "status", "--probe"]
    if sys.platform != "darwin":
        command = ["/usr/bin/xvfb-run", "-a", *command]
    try:
        result = runner(
            command,
            cwd=settings.root,
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
    except (OSError, subprocess.SubprocessError):
        logger.exception("Xiaohongshu session probe could not run")
        return False
    return result.returncode == 0


def trigger_session_refresh(
    settings: Settings,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    """Ask the user service manager to run the singleton refresh job."""
    if not settings.xiaohongshu_session_enabled:
        return False
    command = [
        "systemctl",
        "--user",
        "start",
        "--no-block",
        "xhs-session-manager.service",
    ]
    if sys.platform == "darwin":
        command = [
            "launchctl",
            "kickstart",
            "-k",
            f"gui/{os.getuid()}/com.rag-favorite.xhs-session-manager",
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
        logger.exception("could not trigger Xiaohongshu session refresh")
        return False
    if result.returncode != 0:
        logger.warning("Xiaohongshu session refresh trigger was rejected")
        return False
    return True
