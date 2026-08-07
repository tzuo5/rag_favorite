from __future__ import annotations

import argparse
import http.cookiejar
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from backend.ingestion.config import Settings
from backend.ingestion.logging import configure_logging
from backend.xhs_session.lock import SessionAlreadyRunning, SessionLock
from backend.xhs_session.login_probe import SessionStatus
from backend.xhs_session.state import (
    ensure_private_directory,
    read_status,
    utc_timestamp,
    write_status,
)

from .cookie_export import (
    CookieExportError,
    atomic_export_cookies,
    validate_cookie_file,
)
from .notifier import LoginNotifier

logger = logging.getLogger(__name__)

GENERATE_URL = (
    "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
)
POLL_URL = (
    "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
)
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Manage Bilibili QR login")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("login")
    return root


class SessionManager:
    def __init__(
        self,
        settings: Settings,
        *,
        notifier: LoginNotifier | None = None,
        repository_factory: Callable[[Settings], Any] | None = None,
    ):
        self.settings = settings
        self.notifier = notifier or LoginNotifier(
            settings.bilibili_login_chat_ids
        )
        self.repository_factory = repository_factory

    def _prepare(self) -> None:
        for path in (
            self.settings.bilibili_session_root,
            self.settings.bilibili_status_file.parent,
            self.settings.bilibili_session_lock_file.parent,
            self.settings.bilibili_qr_file.parent,
        ):
            ensure_private_directory(path)

    def _record(
        self,
        status: SessionStatus,
        *,
        file_status: str,
    ) -> dict[str, Any]:
        previous = read_status(self.settings.bilibili_status_file)
        now = utc_timestamp()
        return write_status(self.settings.bilibili_status_file, {
            **previous,
            "file_status": file_status,
            "session_status": status.value,
            "last_probe_at": now,
            "last_success_at": (
                now
                if status == SessionStatus.SESSION_VALID
                else previous.get("last_success_at")
            ),
            "manual_login_required": status == SessionStatus.AUTH_EXPIRED,
        })

    def _cookie_jar(self) -> http.cookiejar.MozillaCookieJar:
        jar = http.cookiejar.MozillaCookieJar()
        value = self.settings.bilibili_cookies_file
        if value:
            path = Path(value)
            try:
                validate_cookie_file(path)
                jar.load(path, ignore_discard=True, ignore_expires=True)
            except (CookieExportError, OSError, http.cookiejar.LoadError):
                pass
        return jar

    @staticmethod
    def _json(
        opener: urllib.request.OpenerDirector,
        url: str,
    ) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=HEADERS)
        with opener.open(request, timeout=30) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise TypeError("Bilibili returned invalid JSON")
        return payload

    def status(self) -> dict[str, Any]:
        self._prepare()
        jar = self._cookie_jar()
        file_status = (
            "ready"
            if any(cookie.name == "SESSDATA" for cookie in jar)
            else "missing"
        )
        if file_status == "missing":
            return self._record(
                SessionStatus.AUTH_EXPIRED,
                file_status=file_status,
            )
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
        try:
            payload = self._json(opener, NAV_URL)
            valid = (
                payload.get("code") == 0
                and isinstance(payload.get("data"), dict)
                and payload["data"].get("isLogin") is True
            )
            status = (
                SessionStatus.SESSION_VALID
                if valid
                else SessionStatus.AUTH_EXPIRED
            )
        except (
            OSError,
            TypeError,
            ValueError,
            RuntimeError,
            urllib.error.URLError,
        ):
            status = SessionStatus.NETWORK_ERROR
        return self._record(status, file_status=file_status)

    def _resume_work(self) -> dict[str, int]:
        counts = {
            "resumed_batch_count": 0,
            "resumed_job_count": 0,
            "resumed_discovery_count": 0,
        }
        if not self.settings.bilibili_auto_resume:
            return counts
        try:
            if self.repository_factory is None:
                from backend.ingestion.repository import SqlRepository

                repository = SqlRepository(self.settings)
            else:
                repository = self.repository_factory(self.settings)
        except Exception:
            logger.exception("could not open Bilibili recovery repository")
            return counts
        recoveries = (
            (
                "resumed_batch_count",
                repository.resume_recovered_bilibili_batches,
            ),
            (
                "resumed_job_count",
                repository.resume_recovered_bilibili_jobs,
            ),
            (
                "resumed_discovery_count",
                repository.resume_recovered_bilibili_discoveries,
            ),
        )
        for field, recovery in recoveries:
            try:
                counts[field] = int(recovery())
            except Exception:
                logger.exception(
                    "could not resume one Bilibili work category",
                    extra={"recovery_field": field},
                )
        return counts

    def login(self) -> dict[str, Any]:
        self._prepare()
        if not self.settings.bilibili_cookies_file:
            raise CookieExportError("BILIBILI_COOKIES_FILE is not configured")
        with SessionLock(self.settings.bilibili_session_lock_file):
            jar = http.cookiejar.MozillaCookieJar()
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar)
            )
            generated = self._json(opener, GENERATE_URL)
            data = generated.get("data")
            if generated.get("code") != 0 or not isinstance(data, dict):
                raise RuntimeError("Bilibili QR generation failed")
            qr_url = str(data.get("url") or "")
            qr_key = str(data.get("qrcode_key") or "")
            if not qr_url or not qr_key:
                raise RuntimeError("Bilibili QR response was incomplete")
            qr_path = self.settings.bilibili_qr_file
            try:
                import qrcode

                image = qrcode.make(qr_url)
                image.save(qr_path)
                qr_path.chmod(0o600)
                messages = self.notifier.send_qr(qr_path)
                deadline = time.monotonic() + min(
                    self.settings.bilibili_login_timeout_seconds,
                    self.settings.bilibili_qr_ttl_seconds,
                )
                last_code: int | None = None
                while time.monotonic() < deadline:
                    query = urllib.parse.urlencode({"qrcode_key": qr_key})
                    payload = self._json(opener, f"{POLL_URL}?{query}")
                    result = payload.get("data")
                    last_code = (
                        int(result.get("code"))
                        if isinstance(result, dict)
                        and str(result.get("code", "")).lstrip("-").isdigit()
                        else None
                    )
                    if payload.get("code") == 0 and last_code == 0:
                        atomic_export_cookies(
                            Path(self.settings.bilibili_cookies_file),
                            jar,
                        )
                        # The QR poll result alone is not sufficient evidence:
                        # probe the authenticated nav endpoint using the
                        # published file before resuming paused ingestion.
                        recorded = self.status()
                        if (
                            recorded.get("session_status")
                            == SessionStatus.SESSION_VALID.value
                        ):
                            recorded.update(self._resume_work())
                            self.notifier.login_succeeded(messages)
                        return recorded
                    if last_code == 86038:
                        break
                    time.sleep(2)
                return self._record(
                    SessionStatus.AUTH_EXPIRED,
                    file_status="missing",
                )
            finally:
                qr_path.unlink(missing_ok=True)


def main() -> int:
    configure_logging()
    args = parser().parse_args()
    manager = SessionManager(Settings())
    try:
        result = (
            manager.login()
            if args.command == "login"
            else manager.status()
        )
    except (
        CookieExportError,
        OSError,
        RuntimeError,
        SessionAlreadyRunning,
    ) as exc:
        print(json.dumps({
            "ok": False,
            "status": type(exc).__name__,
        }))
        return 2
    result = {
        "ok": result.get("session_status") == SessionStatus.SESSION_VALID.value,
        **result,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
