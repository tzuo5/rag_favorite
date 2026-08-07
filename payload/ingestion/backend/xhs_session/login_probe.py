from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class SessionStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    FILE_MISSING = "FILE_MISSING"
    FILE_INCOMPLETE = "FILE_INCOMPLETE"
    SESSION_VALID = "SESSION_VALID"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    RATE_LIMITED = "RATE_LIMITED"
    BOT_CHECK = "BOT_CHECK"
    PLATFORM_BLOCKED = "PLATFORM_BLOCKED"
    NETWORK_ERROR = "NETWORK_ERROR"
    PROFILE_CORRUPT = "PROFILE_CORRUPT"


@dataclass(frozen=True)
class ProbeResult:
    status: SessionStatus

    @property
    def manual_login_required(self) -> bool:
        return self.status == SessionStatus.AUTH_EXPIRED


def _status_from_payload(payload: Any) -> SessionStatus:
    if not isinstance(payload, dict):
        return SessionStatus.UNKNOWN
    if payload.get("success") is True and isinstance(payload.get("data"), dict):
        return SessionStatus.SESSION_VALID
    code = payload.get("code")
    if code in {-100, 401}:
        return SessionStatus.AUTH_EXPIRED
    if code in {429, 300011}:
        return SessionStatus.RATE_LIMITED
    if code in {300012, 300013}:
        return SessionStatus.BOT_CHECK
    if code in {300015, 403}:
        return SessionStatus.PLATFORM_BLOCKED
    return SessionStatus.UNKNOWN


def probe_browser_context(context: Any, page: Any) -> ProbeResult:
    """Perform a low-frequency authenticated request without retaining content."""
    try:
        response = context.request.get(
            "https://edith.xiaohongshu.com/api/sns/web/v1/user/selfinfo",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.xiaohongshu.com/",
            },
            timeout=30_000,
        )
        status_code = int(response.status)
        if status_code == 429:
            return ProbeResult(SessionStatus.RATE_LIMITED)
        if status_code == 401:
            return ProbeResult(SessionStatus.AUTH_EXPIRED)
        if status_code == 403:
            return ProbeResult(SessionStatus.PLATFORM_BLOCKED)
        try:
            status = _status_from_payload(response.json())
        except Exception:  # noqa: BLE001 - response bodies are untrusted
            status = SessionStatus.UNKNOWN
        if status != SessionStatus.UNKNOWN:
            return ProbeResult(status)
    except Exception:  # noqa: BLE001 - normalize browser/network failures
        return ProbeResult(SessionStatus.NETWORK_ERROR)

    # Xiaohongshu can return HTTP 406/code -1 for an unsigned selfinfo request
    # even while the interactive browser is authenticated. Treat the browser's
    # own logged-in navigation plus the two required cookies as the fallback
    # positive signal. A visible login UI remains an explicit negative signal.
    try:
        login_visible = page.locator(
            "[class*='login-container'], [class*='login-modal'], "
            "[class*='qrcode'], [class*='qr-code']"
        ).first.is_visible(timeout=1_000)
    except Exception:  # noqa: BLE001
        login_visible = False
    if login_visible:
        return ProbeResult(SessionStatus.AUTH_EXPIRED)

    try:
        cookie_names = {
            str(cookie.get("name"))
            for cookie in context.cookies()
            if "xiaohongshu.com" in str(cookie.get("domain", ""))
            and cookie.get("value")
        }
        authenticated_navigation_visible = page.locator(
            "li.user.side-bar-component a[href*='/user/profile/']"
        ).first.is_visible(timeout=1_000)
    except Exception:  # noqa: BLE001 - browser state is untrusted
        authenticated_navigation_visible = False
        cookie_names = set()
    if (
        {"a1", "web_session"} <= cookie_names
        and authenticated_navigation_visible
    ):
        return ProbeResult(SessionStatus.SESSION_VALID)
    return ProbeResult(SessionStatus.UNKNOWN)
