from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Self

from backend.ingestion.config import Settings

from .login_probe import ProbeResult, SessionStatus, probe_browser_context
from .state import ensure_private_directory

HOME_URL = "https://www.xiaohongshu.com/explore"
QR_SELECTORS = (
    "[class*='qrcode'] canvas",
    "[class*='qrcode'] img",
    "[class*='qr-code'] canvas",
    "[class*='qr-code'] img",
    "canvas[class*='qr']",
    "img[src*='qr']",
)
LOGIN_SELECTORS = (
    "button:has-text('登录')",
    "[class*='login-btn']",
    "[class*='login-button']",
)


class BrowserUnavailable(RuntimeError):
    pass


class QrCodeUnavailable(RuntimeError):
    pass


class ProfileCorrupt(BrowserUnavailable):
    pass


class BrowserSession:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright: Any | None = None
        self.context: Any | None = None
        self.page: Any | None = None

    def open(self) -> BrowserSession:
        raise BrowserUnavailable(
            "XHS_SESSION_SERVICE_REQUIRED: 请使用 RAG 网页中的登录与验证入口。"
        )

    def close(self) -> None:
        if self.context is not None:
            try:
                self.context.close()
            except Exception:  # noqa: BLE001, S110
                pass
            self.context = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # noqa: BLE001, S110
                pass
            self._playwright = None
        self.page = None

    def __enter__(self) -> Self:
        return self.open()

    def __exit__(self, *args: object) -> None:
        self.close()

    def probe(self) -> ProbeResult:
        if self.context is None or self.page is None:
            raise BrowserUnavailable("browser session is not open")
        return probe_browser_context(self.context, self.page)

    def cookies(self) -> list[dict[str, Any]]:
        if self.context is None:
            raise BrowserUnavailable("browser session is not open")
        return list(self.context.cookies())

    def capture_login_qr(self, path: Path) -> None:
        if self.page is None:
            raise BrowserUnavailable("browser session is not open")
        for selector in LOGIN_SELECTORS:
            try:
                locator = self.page.locator(selector).first
                if locator.is_visible(timeout=1_000):
                    locator.click(timeout=3_000)
                    break
            except Exception:  # noqa: BLE001, S112
                continue
        for selector in QR_SELECTORS:
            try:
                locator = self.page.locator(selector).first
                locator.wait_for(state="visible", timeout=8_000)
                ensure_private_directory(path.parent)
                locator.screenshot(path=str(path))
                path.chmod(0o600)
                return
            except Exception:  # noqa: BLE001, S112
                continue
        raise QrCodeUnavailable("login QR code was not available")

    def wait_for_login(self, timeout_seconds: int) -> ProbeResult:
        deadline = time.monotonic() + timeout_seconds
        last = ProbeResult(SessionStatus.UNKNOWN)
        while time.monotonic() < deadline:
            last = self.probe()
            if last.status == SessionStatus.SESSION_VALID:
                return last
            if last.status in {
                SessionStatus.RATE_LIMITED,
                SessionStatus.BOT_CHECK,
                SessionStatus.PLATFORM_BLOCKED,
            }:
                return last
            time.sleep(2)
        return last
