from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from backend.ingestion.config import Settings
from backend.ingestion.discovery.xiaohongshu import cookie_file_diagnostics
from backend.ingestion.logging import configure_logging

from .browser import (
    BrowserSession,
    BrowserUnavailable,
    ProfileCorrupt,
    QrCodeUnavailable,
)
from .cookie_export import CookieExportError, atomic_export_cookies
from .lock import SessionAlreadyRunning, SessionLock, cleanup_stale_lock
from .login_probe import ProbeResult, SessionStatus
from .notifier import LoginNotifier
from .state import (
    ensure_private_directory,
    read_status,
    utc_timestamp,
    write_status,
)

logger = logging.getLogger(__name__)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Manage the persistent Xiaohongshu login session"
    )
    commands = root.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status")
    status.add_argument(
        "--probe",
        action="store_true",
        help="open Chromium and perform a real low-frequency session probe",
    )
    commands.add_parser("refresh")
    commands.add_parser("login")
    commands.add_parser("export-cookies")
    commands.add_parser("cleanup")
    return root


class SessionManager:
    def __init__(
        self,
        settings: Settings,
        *,
        browser_factory: Callable[[Settings], Any] = BrowserSession,
        notifier: LoginNotifier | None = None,
        repository_factory: Callable[[Settings], Any] | None = None,
    ):
        self.settings = settings
        self.browser_factory = browser_factory
        self.notifier = notifier or LoginNotifier(settings.xiaohongshu_login_chat_ids)
        self.repository_factory = repository_factory

    def _prepare(self) -> None:
        for path in (
            self.settings.xiaohongshu_session_root,
            self.settings.xiaohongshu_profile_dir,
            self.settings.xiaohongshu_status_file.parent,
            self.settings.xiaohongshu_session_lock_file.parent,
            self.settings.xiaohongshu_qr_file.parent,
        ):
            ensure_private_directory(path)

    def _file_status(self) -> str:
        return str(cookie_file_diagnostics(self.settings)["file_status"])

    def _record(self, result: ProbeResult) -> dict[str, Any]:
        previous = read_status(self.settings.xiaohongshu_status_file)
        now = utc_timestamp()
        status = {
            **previous,
            "file_status": self._file_status(),
            "session_status": result.status.value,
            "last_probe_at": now,
            "last_success_at": (
                now
                if result.status == SessionStatus.SESSION_VALID
                else previous.get("last_success_at")
            ),
            "manual_login_required": result.manual_login_required,
        }
        return write_status(self.settings.xiaohongshu_status_file, status)

    def status(self, *, probe: bool = False) -> dict[str, Any]:
        self._prepare()
        if probe:
            with (
                SessionLock(self.settings.xiaohongshu_session_lock_file),
                self.browser_factory(self.settings) as browser,
            ):
                return self._record(browser.probe())
        result = read_status(self.settings.xiaohongshu_status_file)
        result["file_status"] = self._file_status()
        if result["session_status"] == SessionStatus.UNKNOWN.value:
            if result["file_status"] == "missing":
                result["session_status"] = SessionStatus.FILE_MISSING.value
            elif result["file_status"] != "ready":
                result["session_status"] = SessionStatus.FILE_INCOMPLETE.value
        return result

    def _publish(self, browser: Any) -> dict[str, Any]:
        before = browser.probe()
        if before.status != SessionStatus.SESSION_VALID:
            return self._record(before)
        if not self.settings.xiaohongshu_cookies_file:
            raise CookieExportError("XIAOHONGSHU_COOKIES_FILE is not configured")
        atomic_export_cookies(
            Path(self.settings.xiaohongshu_cookies_file),
            browser.cookies(),
        )
        # File completeness is not treated as authentication evidence.
        after = browser.probe()
        result = self._record(after)
        if after.status == SessionStatus.SESSION_VALID:
            result.update(self._resume_work())
        return result

    def _resume_work(self) -> dict[str, int]:
        counts = {
            "resumed_batch_count": 0,
            "resumed_job_count": 0,
            "resumed_discovery_count": 0,
        }
        if not self.settings.xiaohongshu_auto_resume:
            return counts
        try:
            if self.repository_factory is None:
                from backend.ingestion.repository import SqlRepository

                repository = SqlRepository(self.settings)
            else:
                repository = self.repository_factory(self.settings)
        except Exception:
            logger.exception("could not open Xiaohongshu recovery repository")
            return counts
        recoveries = (
            (
                "resumed_batch_count",
                repository.resume_recovered_xiaohongshu_batches,
            ),
            (
                "resumed_job_count",
                repository.resume_recovered_xiaohongshu_jobs,
            ),
            (
                "resumed_discovery_count",
                repository.resume_recovered_xiaohongshu_discoveries,
            ),
        )
        for field, recovery in recoveries:
            try:
                counts[field] = int(recovery())
            except Exception:
                logger.exception(
                    "could not resume one Xiaohongshu work category",
                    extra={"recovery_field": field},
                )
        return counts

    def _login_with_browser(self, browser: Any, initial: ProbeResult) -> dict[str, Any]:
        if initial.status == SessionStatus.SESSION_VALID:
            return self._publish(browser)
        if initial.status in {
            SessionStatus.RATE_LIMITED,
            SessionStatus.BOT_CHECK,
            SessionStatus.PLATFORM_BLOCKED,
            SessionStatus.NETWORK_ERROR,
            SessionStatus.PROFILE_CORRUPT,
        }:
            return self._record(initial)
        # The lightweight self-info probe may return UNKNOWN after the web UI
        # changes even though the only safe recovery is an interactive login.
        # In that state, show a fresh QR instead of silently leaving stale
        # cookies in place.
        qr_path = self.settings.xiaohongshu_qr_file
        qr_path.unlink(missing_ok=True)
        try:
            browser.capture_login_qr(qr_path)
            messages = self.notifier.send_qr(qr_path)
            result = browser.wait_for_login(
                self.settings.xiaohongshu_login_timeout_seconds
            )
            if result.status != SessionStatus.SESSION_VALID:
                return self._record(result)
            published = self._publish(browser)
            if published["session_status"] == SessionStatus.SESSION_VALID.value:
                self.notifier.login_succeeded(messages)
            return published
        finally:
            qr_path.unlink(missing_ok=True)

    def refresh(self) -> dict[str, Any]:
        self._prepare()
        with (
            SessionLock(self.settings.xiaohongshu_session_lock_file),
            self.browser_factory(self.settings) as browser,
        ):
            return self._login_with_browser(browser, browser.probe())

    def login(self) -> dict[str, Any]:
        return self.refresh()

    def export_cookies(self) -> dict[str, Any]:
        self._prepare()
        with (
            SessionLock(self.settings.xiaohongshu_session_lock_file),
            self.browser_factory(self.settings) as browser,
        ):
            return self._publish(browser)

    def cleanup(self) -> dict[str, Any]:
        self._prepare()
        removed = 0
        qr_path = self.settings.xiaohongshu_qr_file
        try:
            qr_age = time.time() - qr_path.stat().st_mtime
        except OSError:
            qr_age = 0
        if qr_age >= self.settings.xiaohongshu_qr_ttl_seconds:
            qr_path.unlink(missing_ok=True)
            removed += 1
        for parent in (
            self.settings.xiaohongshu_status_file.parent,
            self.settings.xiaohongshu_qr_file.parent,
        ):
            for path in parent.glob(".*.tmp"):
                try:
                    if time.time() - path.stat().st_mtime >= 3600:
                        path.unlink()
                        removed += 1
                except OSError:
                    continue
        stale_lock_removed = cleanup_stale_lock(
            self.settings.xiaohongshu_session_lock_file
        )
        return {
            "removed_file_count": removed,
            "stale_lock_removed": stale_lock_removed,
        }


def _safe_error_status(exc: Exception) -> str:
    if isinstance(exc, SessionAlreadyRunning):
        return "already_running"
    if isinstance(exc, ProfileCorrupt):
        return "profile_corrupt"
    if isinstance(exc, BrowserUnavailable):
        return "browser_unavailable"
    if isinstance(exc, QrCodeUnavailable):
        return "qr_unavailable"
    if isinstance(exc, CookieExportError):
        return "cookie_export_failed"
    return "session_manager_failed"


def main() -> int:
    configure_logging()
    args = parser().parse_args()
    manager = SessionManager(Settings())
    try:
        if args.command == "status":
            result = manager.status(probe=args.probe)
        elif args.command == "refresh":
            result = manager.refresh()
        elif args.command == "login":
            result = manager.login()
        elif args.command == "export-cookies":
            result = manager.export_cookies()
        else:
            result = manager.cleanup()
    except (
        BrowserUnavailable,
        CookieExportError,
        QrCodeUnavailable,
        SessionAlreadyRunning,
    ) as exc:
        if isinstance(exc, ProfileCorrupt):
            manager._record(ProbeResult(SessionStatus.PROFILE_CORRUPT))
        result = {"ok": False, "status": _safe_error_status(exc)}
        print(json.dumps(result, ensure_ascii=False))
        return 2
    requires_valid_session = args.command in {"refresh", "login", "export-cookies"} or (
        args.command == "status" and args.probe
    )
    result = {
        "ok": (
            result.get("session_status") == SessionStatus.SESSION_VALID.value
            if requires_valid_session
            else True
        ),
        **result,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
