"""Opt-in real Chrome tests using entirely synthetic, intercepted login pages."""

import asyncio
import base64
import os
import time
from types import SimpleNamespace

import pytest

from rag_favorite.video_provider import ProviderUnavailable
from rag_favorite.xhs_private import browser_session_lock
from rag_favorite.xhs_session_service import SessionService

pytestmark = pytest.mark.skipif(
    os.environ.get("RAG_BROWSER_TEST") != "1", reason="Opt-in local Chrome fixture"
)


def test_phone_qr_pointer_sms_and_profile_restart(tmp_path):
    import cv2
    import numpy as np
    from playwright.async_api import async_playwright

    link = "https://www.xiaohongshu.com/website-login/qr?test=synthetic"
    qr = cv2.QRCodeEncoder_create().encode(link)
    qr = cv2.resize(
        np.pad(qr, 4, constant_values=255),
        None,
        fx=8,
        fy=8,
        interpolation=cv2.INTER_NEAREST,
    )
    encoded = base64.b64encode(cv2.imencode(".png", qr)[1]).decode()
    html = (
        """<html><head><meta charset="utf-8"></head><body><div class="login-container" style="position:absolute;left:40px;top:40px;width:600px;height:740px;background:white">
      <h1>登录</h1><img class="qrcode-img" src="data:image/png;base64,"""
        + encoded
        + """">
      <input placeholder="验证码"><button onclick="window.submitted=document.querySelector('input').value">确认</button>
      </div><script>window.moves=[];document.addEventListener('pointermove',e=>window.moves.push([e.clientX,e.clientY]));</script></body></html>"""
    )
    video = SimpleNamespace(root=tmp_path / "data")

    async def run():
        async with async_playwright() as playwright:
            s = SessionService(video)
            with browser_session_lock(s.root):
                with (
                    pytest.raises(ProviderUnavailable, match="SESSION_BUSY"),
                    browser_session_lock(s.root, timeout=0),
                ):
                    pytest.fail("Second browser owner acquired the same profile")
                await s.start(playwright)
                await s.context.route(
                    "**/*",
                    lambda route: route.fulfill(
                        status=200, content_type="text/html", body=html
                    ),
                )
                await s.begin_login()
                attempt = s.login["id"]
                frame = await s.login_frame({"attempt_id": attempt})
                assert frame["qr_link"] == link
                assert frame["qr"] and frame["image"]
                params = {"attempt_id": attempt, "frame_id": frame["frame_id"]}
                with pytest.raises(ProviderUnavailable, match="INTERACTION_INVALID"):
                    await s.interact("pointer", {**params, "points": [[1000, 800]]})
                await s.interact(
                    "pointer",
                    {**params, "points": [[100, 700], [180, 700], [240, 700]]},
                )
                assert (await s.page.evaluate("window.moves"))[-1] == [240, 700]
                with pytest.raises(ProviderUnavailable, match="INTERACTION_EXPIRED"):
                    await s.interact("otp", {**params, "code": "123456"})
                frame = await s.login_frame({"attempt_id": attempt})
                params = {
                    "attempt_id": attempt,
                    "frame_id": frame["frame_id"],
                    "code": "123456",
                }
                await s.interact("otp", params)
                assert "code" not in params
                assert await s.page.evaluate("window.submitted") == "123456"
                await s.context.add_cookies(
                    [
                        {
                            "name": "test_persistent",
                            "value": "fixture",
                            "domain": ".xiaohongshu.com",
                            "path": "/",
                            "expires": time.time() + 86400,
                        }
                    ]
                )
                await s.page.evaluate("localStorage.setItem('fixture','preserved')")
                await s.export()
                await s.context.close()
            restored = SessionService(video)
            with browser_session_lock(restored.root):
                await restored.start(playwright)
                await restored.context.route(
                    "**/*",
                    lambda route: route.fulfill(
                        status=200, content_type="text/html", body=html
                    ),
                )
                await restored.page.goto("https://www.xiaohongshu.com/explore")
                assert (
                    await restored.page.evaluate("localStorage.getItem('fixture')")
                    == "preserved"
                )
                assert any(
                    c["name"] == "test_persistent"
                    for c in await restored.context.cookies()
                )
                await restored.context.close()

    asyncio.run(run())


def test_mobile_console_layout_and_safe_login_rendering(tmp_path):
    import json
    import shutil
    from pathlib import Path

    from playwright.async_api import async_playwright
    from test_web_console import SNAPSHOT

    from rag_favorite.web_console import public_snapshot

    assets = Path(__file__).resolve().parents[1] / "src/rag_favorite/resources/web"
    screenshot = Path("/tmp/xhs-phone-console-fixture.png")
    actions = []
    state = {
        "service": "ready",
        "paths": {"note": {"state": "verification_required"}},
        "counters": {},
        "login": None,
    }

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                executable_path=shutil.which("google-chrome"), headless=True
            )
            page = await browser.new_page(
                viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
            )
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            async def fixture(route):
                from urllib.parse import urlsplit

                path = urlsplit(route.request.url).path
                if path == "/":
                    return await route.fulfill(
                        content_type="text/html; charset=utf-8",
                        body=(assets / "index.html").read_text(),
                    )
                if path.startswith("/assets/"):
                    asset = assets / path.split("/")[-1]
                    return await route.fulfill(
                        content_type="text/css"
                        if asset.suffix == ".css"
                        else "application/javascript",
                        body=asset.read_text(),
                    )
                if path == "/api/session":
                    data = {"csrf": "fixture-token"}
                elif path == "/api/snapshot":
                    data = {"snapshot": public_snapshot(SNAPSHOT), "stale": False}
                elif path == "/api/xhs/session":
                    data = state
                elif path == "/api/xhs/action":
                    actions.append(route.request.post_data_json)
                    assert route.request.headers["x-csrf-token"] == "fixture-token"
                    data = state
                else:
                    return await route.abort()
                await route.fulfill(
                    content_type="application/json", body=json.dumps(data)
                )

            await page.route("**/*", fixture)
            await page.goto("https://console.example.ts.net/")
            await page.locator('[data-view="account"]').click()
            await (
                page.locator("#xhs-paths")
                .get_by_text("需要手机验证", exact=False)
                .wait_for()
            )
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth"
            )
            await page.locator("#xhs-login").click()
            assert actions[-1]["action"] == "login_start"
            await page.locator("#xhs-panel").screenshot(path=str(screenshot))
            assert not errors
            await browser.close()

    asyncio.run(run())
