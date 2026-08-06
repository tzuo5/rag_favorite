from __future__ import annotations

import re
import subprocess
import time
from typing import Any

AUTHOR_BATCH_UNITS = (
    "video-author-discovery-worker.service",
    "video-ingestion-worker.service",
    "video-batch-notification-worker.service",
    "openclaw-gateway.service",
)
_ERROR_RE = re.compile(
    r"\b(error|exception|traceback|failed|critical)\b",
    re.IGNORECASE,
)
_RAW_SECRET_RE = re.compile(
    r"""(?ix)
    (?:xsec_token|web_session|authorization|cookie)
    \s*[:=]\s*(?!\[?redacted\]?)
    [^\s,;]+
    |
    https://[^\s]+\.xhscdn\.com/(?!\[redacted\])
    """,
)


def collect_service_observation(hours: int) -> dict[str, Any]:
    """Return count-only service/journal evidence without exposing log lines."""
    if not 1 <= hours <= 168:
        raise ValueError("observation hours must be between 1 and 168")
    services = {
        unit: _service_observation(unit, hours)
        for unit in AUTHOR_BATCH_UNITS
    }
    active = all(item["active"] for item in services.values())
    window_complete = all(
        item["active_age_seconds"] >= hours * 3600
        for item in services.values()
    )
    journal_available = all(
        item["journal"]["available"] for item in services.values()
    )
    raw_secret_hits = sum(
        item["journal"]["raw_secret_hit_count"]
        for item in services.values()
    )
    return {
        "requested_hours": hours,
        "services": services,
        "all_services_active": active,
        "observation_window_complete": window_complete,
        "journal_available": journal_available,
        "raw_secret_hit_count": raw_secret_hits,
        "ok": active and journal_available and raw_secret_hits == 0,
    }


def _service_observation(unit: str, hours: int) -> dict[str, Any]:
    properties = _run([
        "systemctl",
        "--user",
        "show",
        unit,
        "--property=ActiveState",
        "--property=ActiveEnterTimestampMonotonic",
        "--no-pager",
    ])
    values = {}
    if properties.returncode == 0:
        for line in properties.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
    active = values.get("ActiveState") == "active"
    try:
        entered = int(values.get("ActiveEnterTimestampMonotonic") or 0)
    except ValueError:
        entered = 0
    active_age_seconds = max(
        0,
        int(time.clock_gettime(time.CLOCK_BOOTTIME) - entered / 1_000_000),
    ) if active and entered else 0
    requested_start_epoch = time.time() - hours * 3600
    active_start_epoch = time.time() - active_age_seconds
    journal_start_epoch = max(requested_start_epoch, active_start_epoch)

    journal = _run([
        "journalctl",
        "--user",
        "--unit",
        unit,
        "--since",
        f"@{int(journal_start_epoch)}",
        "--lines",
        "10000",
        "--output=cat",
        "--no-pager",
    ])
    log_text = journal.stdout if journal.returncode == 0 else ""
    lines = log_text.splitlines()
    return {
        "active": active,
        "active_age_seconds": active_age_seconds,
        "journal": {
            "available": journal.returncode == 0,
            "line_count": len(lines),
            "error_line_count": sum(
                bool(_ERROR_RE.search(line)) for line in lines
            ),
            "raw_secret_hit_count": len(_RAW_SECRET_RE.findall(log_text)),
            "line_limit": 10000,
        },
    }


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr=type(exc).__name__,
        )
