"""Owner-controlled QR login and automatic private Xiaohongshu cookie export."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ingestion"))
from backend.xhs_session.cookie_export import atomic_export_cookies
from backend.xhs_session.login_probe import (
    SessionStatus,
    probe_browser_context,
)

from rag_favorite.xhs_private import browser_session_lock


def main():
    from playwright.sync_api import sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    root = REPO / ".runtime/videorag/xhs-session"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    status = root / "status.json"
    with browser_session_lock(root), sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(root / "profile"),
            executable_path="/usr/bin/google-chrome",
            headless=not args.headed,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
            args=[
                "--disable-dev-shm-usage",
                "--remote-debugging-port=0",
                "--remote-debugging-address=127.0.0.1",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(
                "https://www.xiaohongshu.com/explore",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            for selector in [
                "button:has-text('登录')",
                "[class*='login-btn']",
                "[class*='login-button']",
            ]:
                locator = page.locator(selector).first
                if locator.is_visible():
                    locator.click(timeout=3000)
                    break
            page.wait_for_timeout(15000)
            print(
                "Login page title:",
                page.title(),
                "body characters:",
                len(page.locator("body").inner_text()),
                flush=True,
            )
            qr = root / "login-qr.png"
            captured = False
            for selector in [
                "[class*='qrcode'] canvas",
                "[class*='qrcode'] img",
                "[class*='qr-code'] canvas",
                "[class*='qr-code'] img",
                "canvas[class*='qr']",
                "img[src*='qr']",
                ".qrcode-img",
            ]:
                locator = page.locator(selector).first
                if locator.is_visible():
                    locator.screenshot(path=str(qr))
                    captured = True
                    break
            if not captured:
                page.screenshot(path=str(qr))
            qr.chmod(0o600)
            status.write_text(
                json.dumps(
                    {
                        "state": "awaiting_owner_login",
                        "qr_file": str(qr),
                        "cropped_qr": captured,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            status.chmod(0o600)
            print("QR saved:", qr, flush=True)
            print(
                "Use the owner's Xiaohongshu app to scan and confirm login. No password is recorded.",
                flush=True,
            )
            deadline = time.monotonic() + args.timeout
            last_capture = 0.0
            otp_file = root / "pending-otp.txt"
            while time.monotonic() < deadline:
                if otp_file.exists():
                    code_input = page.locator(
                        "input[placeholder*='验证码'], input[autocomplete='one-time-code']"
                    ).first
                    if code_input.is_visible():
                        code = otp_file.read_text().strip()
                        otp_file.unlink()
                        if code.isdigit() and 4 <= len(code) <= 8:
                            code_input.fill(code)
                            del code
                            for label in ("验证", "确认", "登录"):
                                button = page.get_by_role(
                                    "button", name=label, exact=True
                                )
                                if (
                                    button.count() == 1
                                    and button.is_visible()
                                    and button.is_enabled()
                                ):
                                    button.click()
                                    break
                            print(
                                "Owner-provided verification code submitted.",
                                flush=True,
                            )
                        else:
                            del code
                if time.monotonic() - last_capture > 15:
                    page.screenshot(path=str(root / "login-state.png"))
                    (root / "login-state.png").chmod(0o600)
                    last_capture = time.monotonic()
                cookies = context.cookies()
                names = {
                    c["name"]
                    for c in cookies
                    if c.get("value")
                    and (
                        c["domain"].lstrip(".") == "xiaohongshu.com"
                        or c["domain"].endswith(".xiaohongshu.com")
                    )
                }
                navigation = page.locator(
                    "li.user.side-bar-component a[href*='/user/profile/']"
                ).first
                if (
                    {"a1", "web_session"}.issubset(names)
                    and navigation.is_visible()
                    and probe_browser_context(context, page).status
                    == SessionStatus.SESSION_VALID
                ):
                    atomic_export_cookies(root / "cookies.txt", cookies)
                    status.write_text(
                        json.dumps(
                            {
                                "state": "cookies_exported",
                                "required_cookie_names_present": True,
                                "cookie_file": "cookies.txt",
                            },
                            indent=2,
                        )
                    )
                    print("Xiaohongshu cookies exported privately (0600).", flush=True)
                    return
                time.sleep(2)
            status.write_text(
                json.dumps({"state": "login_timeout", "next_action": "rerun QR login"})
            )
            print("Login wait timed out; rerun to generate a fresh QR.", flush=True)
        finally:
            (root / "pending-otp.txt").unlink(missing_ok=True)
            context.close()


if __name__ == "__main__":
    main()
