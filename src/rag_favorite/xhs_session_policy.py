"""Durable account pacing and evidence-based session states, without credentials."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from .video_provider import ProviderUnavailable
from .xhs_private import atomic_private_json, private_directory, private_json

PATHS = ("owner", "favorites", "following", "author", "note", "media", "login", "page")
INTERVAL = 60
OPERATIONS_PER_HOUR = 30
RISK_CODES = {
    "XHS_AUTH_REQUIRED",
    "XHS_VERIFICATION_REQUIRED",
    "XHS_RATE_LIMITED",
    "XHS_PLATFORM_BLOCKED",
    "XHS_SESSION_UNKNOWN",
    "XHS_ACCOUNT_MISMATCH",
}
WAIT_CODES = {
    "XHS_DEFERRED",
    "XHS_SESSION_BUSY",
    "XHS_SESSION_UNAVAILABLE",
    "XHS_AUTH_REQUIRED",
    "XHS_VERIFICATION_REQUIRED",
    "XHS_RATE_LIMITED",
    "XHS_PLATFORM_BLOCKED",
    "XHS_SESSION_UNKNOWN",
    "XHS_ACCOUNT_MISMATCH",
    "XHS_NETWORK_ERROR",
    "XHS_PROXY_UNAVAILABLE",
    "XHS_PROXY_COOLDOWN",
    "XHS_PROXY_PAUSED",
    "XHS_PROXY_STALE_GENERATION",
    "XHS_ACCEPTANCE_PAUSED",
    "XHS_ACCEPTANCE_FAILED",
}
STATES = {
    "XHS_AUTH_REQUIRED": "auth_required",
    "XHS_VERIFICATION_REQUIRED": "verification_required",
    "XHS_RATE_LIMITED": "rate_limited",
    "XHS_PLATFORM_BLOCKED": "blocked",
    "XHS_SESSION_UNKNOWN": "unknown",
    "XHS_ACCOUNT_MISMATCH": "account_mismatch",
    "XHS_NETWORK_ERROR": "network_error",
}


def retry_after_seconds(value):
    if not value:
        return None
    try:
        delay = (
            float(value)
            if str(value).strip().isdigit()
            else (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        )
        return max(1, int(delay))
    except (ValueError, TypeError, OverflowError):
        return None


def failure(code, *, status=None, platform=None, delay=None):
    details = {}
    if str(platform).lstrip("-").isdigit() and len(str(platform)) <= 12:
        details["platform_code"] = str(platform)
    if delay is not None:
        details["retry_after"] = max(1, int(delay))
    return ProviderUnavailable(
        code,
        code=code,
        status_code=status,
        diagnostics=details,
        retryable=code == "XHS_NETWORK_ERROR",
        error_category=STATES.get(code),
    )


def classify(*, status=None, platform=None, evidence=None):
    """An HTTP rejection alone is never proof that the owner logged out."""
    platform = str(platform) if platform is not None else None
    if platform == "300012" or evidence == "platform_risk":
        code = "XHS_PLATFORM_BLOCKED"
        platform = "300012"
    elif evidence == "verification":
        code = "XHS_VERIFICATION_REQUIRED"
    elif evidence == "login_and_guest":
        code = "XHS_AUTH_REQUIRED"
    elif status == 429:
        code = "XHS_RATE_LIMITED"
    elif evidence == "note_unavailable" or status in {404, 410}:
        code = "XHS_NOTE_UNAVAILABLE"
    elif status is not None and status >= 500:
        code = "XHS_NETWORK_ERROR"
    else:
        code = "XHS_SESSION_UNKNOWN"
    return failure(code, status=status, platform=platform)


class SessionPolicy:
    def __init__(self, root: Path, *, clock=time.time, paced=True):
        private_directory(root)
        self.root, self.clock = root, clock
        self.paced = paced
        path = root / "session-state.json"
        self.data = (
            private_json(path)
            if path.exists()
            else {
                "version": 0,
                "paths": {},
                "last_request": 0,
                "notes": [],
                "counters": {},
                "started_at": clock(),
            }
        )
        # Migrate the old note-only budget conservatively. Neither proxy changes
        # nor a service restart creates a new account budget.
        self.data.setdefault("operations", list(self.data.get("notes", [])))
        self.data.setdefault("recovery", {"phase": "initial", "reason": None})
        if self.data["recovery"]["phase"] == "initial" and any(
            item.get("code") in RISK_CODES for item in self.data["paths"].values()
        ):
            self.pause("XHS_ACCEPTANCE_PAUSED")

    def save(self):
        self.data["version"] += 1
        atomic_private_json(self.root / "session-state.json", self.data)

    def count(self, key):
        counters = self.data.setdefault("counters", {})
        counters[key] = counters.get(key, 0) + 1

    def public(self):
        result = {
            k: self.data.get(k)
            for k in (
                "version",
                "paths",
                "counters",
                "started_at",
                "network",
                "recovery",
            )
        }
        result["budget"] = self.budget()
        return result

    def budget(self, *, now=None):
        now = self.clock() if now is None else now
        operations = [t for t in self.data["operations"] if t > now - 3600]
        notes = [t for t in self.data.get("notes", []) if t > now - 86400]
        hourly = [t for t in notes if t > now - 3600]
        limit = 12 if self.data["recovery"]["phase"] == "steady" else 6
        next_at = self.data.get("last_request", 0) + INTERVAL
        if len(operations) >= OPERATIONS_PER_HOUR:
            next_at = max(next_at, operations[-OPERATIONS_PER_HOUR] + 3600)
        note_at = next_at
        if len(hourly) >= limit:
            note_at = max(note_at, hourly[-limit] + 3600)
        if len(notes) >= 100:
            note_at = max(note_at, notes[-100] + 86400)
        return {
            "interval_seconds": INTERVAL,
            "operations_per_hour": OPERATIONS_PER_HOUR,
            "operations_used": len(operations),
            "operations_remaining": max(0, OPERATIONS_PER_HOUR - len(operations)),
            "notes_per_hour": limit,
            "notes_used": len(hourly),
            "notes_remaining": max(0, limit - len(hourly)),
            "notes_per_24h": 100,
            "notes_used_24h": len(notes),
            "next_operation_at": max(now, next_at),
            "next_note_at": max(now, note_at),
            "scope": "programmatic_page_api_operations",
        }

    def pause(self, reason):
        previous = self.data["recovery"]
        if previous.get("accepted") or previous.get("downloads"):
            history = self.data.setdefault("recovery_history", [])
            history.append(previous)
            self.data["recovery_history"] = history[-10:]
        self.data["recovery"] = {
            "phase": "paused",
            "reason": reason,
            "since": self.clock(),
            "accepted": {},
            "downloads": {},
            "allowed_jobs": [],
            "last_verified_at": previous.get("last_verified_at"),
        }
        self.save()

    def check(self, path, *, interactive=False):
        now = self.clock()
        recovery = self.data["recovery"]
        if not interactive:
            if recovery["phase"] in {"paused", "diagnostic", "validated"}:
                raise failure(
                    recovery.get("reason") or "XHS_ACCEPTANCE_PAUSED", delay=60
                )
            if recovery["phase"] == "acceptance" and path not in {
                "owner",
                "note",
                "media",
            }:
                raise failure("XHS_ACCEPTANCE_PAUSED", delay=60)
        paths = self.data["paths"]
        for key in dict.fromkeys((*paths, path)):
            item = paths.get(key, {})
            if (
                item.get("state") not in {None, "valid", "network_error"}
                and not item.get("cleared_at")
                and not interactive
                and (item.get("retry_at") is None or item["retry_at"] > now)
            ):
                raise failure(
                    item.get("code", "XHS_SESSION_UNKNOWN"),
                    delay=(item.get("retry_at") or now + 1800) - now,
                )

    def reserve(self, path, *, interactive=False):
        self.check(path, interactive=interactive)
        now = self.clock()
        # Media transfers are serial and separately counted; they are not page/API
        # operations. The old paced=False argument remains API-compatible only.
        if path == "media":
            self.count("media_bundles_attempted")
            self.save()
            return
        budget = self.budget(now=now)
        delay = budget["next_note_at" if path == "note" else "next_operation_at"] - now
        if delay > 0:
            raise failure("XHS_DEFERRED", delay=delay + 1)
        self.data.update(
            last_request=now,
            operations=[t for t in self.data["operations"] if t > now - 3600] + [now],
            notes=[t for t in self.data.get("notes", []) if t > now - 86400]
            + ([now] if path == "note" else []),
        )
        self.count(path + "_attempts")
        # Persist BEFORE issuing a request, so a crash cannot erase its budget.
        self.save()

    def success(self, path):
        now = self.clock()
        self.data["paths"][path] = {
            "state": "valid",
            "last_success_at": now,
            "checked_at": now,
        }
        self.count(path + "_successes")
        self.save()

    def reserve_media_note(self, note_id, generation):
        now = self.clock()
        permits = self.data.setdefault("note_permits", {})
        permit = permits.pop(note_id, None)
        if permit and permit["generation"] == generation and now - permit["at"] < 1800:
            self.save()
            return  # The successful page read already charged this attempt.
        budget = self.budget(now=now)
        # A cached source can still initiate a new note download. Charge it to
        # the note quota, without pretending it issued a page/API operation.
        notes = [t for t in self.data.get("notes", []) if t > now - 86400]
        hourly = [t for t in notes if t > now - 3600]
        delay = 0
        if len(hourly) >= budget["notes_per_hour"]:
            delay = hourly[-budget["notes_per_hour"]] + 3600 - now
        if len(notes) >= 100:
            delay = max(delay, notes[-100] + 86400 - now)
        if delay > 0:
            raise failure("XHS_DEFERRED", delay=delay + 1)
        self.data["notes"] = notes + [now]
        self.count("cached_source_note_attempts")
        self.save()

    def failed(self, path, exc):
        code = getattr(exc, "code", None)
        if code not in STATES:
            if code == "XHS_NOTE_UNAVAILABLE":
                self.count(code)
                self.save()
            return
        now = self.clock()
        previous = self.data["paths"].get(path, {})
        state = STATES[code]
        step = previous.get("cooldown_step", 0) + 1
        if state in {"rate_limited", "blocked"}:
            delay = max(
                (1800, 3600, 7200)[min(step - 1, 2)],
                exc.diagnostics.get("retry_after", 0),
            )
        elif state == "unknown":
            delay = 1800 if previous.get("state") != "unknown" else None
        elif state == "network_error":
            delay = 30
        else:
            delay = None
        self.data["paths"][path] = {
            "state": state,
            "code": code,
            "checked_at": now,
            "last_success_at": previous.get("last_success_at"),
            "retry_at": now + delay if delay is not None else None,
            "cooldown_step": step,
            "http_status": exc.status_code,
            "platform_code": exc.diagnostics.get("platform_code"),
            "reason": exc.diagnostics.get("reason"),
        }
        self.count(code)
        if code in RISK_CODES:
            self.pause(code)
        self.save()
        with (self.root / "session-events.jsonl").open("a", encoding="utf-8") as log:
            import json

            log.write(
                json.dumps({"at": now, "path": path, **self.data["paths"][path]}) + "\n"
            )
        (self.root / "session-events.jsonl").chmod(0o600)

    def download_gate(self):
        relevant = [p for p in self.data["paths"].values() if not p.get("cleared_at")]
        blocked = any(
            p.get("state") not in {None, "valid", "network_error"}
            and not p.get("cleared_at")
            and p.get("retry_at") is None
            for p in relevant
        )
        until = max((p.get("retry_at") or 0 for p in relevant), default=0)
        network = self.data.get("network") or {}
        blocked = blocked or network.get("paused", False)
        blocked = blocked or self.data["recovery"]["phase"] in {
            "paused",
            "diagnostic",
            "validated",
            "acceptance",
        }
        until = max(until, network.get("cooldown_until", 0))
        auth = any(p.get("state") == "auth_required" for p in relevant)
        return blocked, until or None, auth
