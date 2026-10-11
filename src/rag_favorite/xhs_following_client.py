"""Discover the owner's following list from the authenticated page's reads."""

from __future__ import annotations

import re
import shutil
from urllib.parse import urlsplit

from .video_provider import ProviderUnavailable
from .xhs_client import NOTE_ID, XhsSessionClient, throttle_account
from .xhs_private import browser_session_lock

FOLLOWING_ALL = "/api/im/web/users/following/all"


def normalize_following(data, expected_count):
    """The IM 'all' response is complete only when the exact profile count agrees."""
    users = data.get("follow_user_d_t_o_list") if isinstance(data, dict) else None
    if (
        not isinstance(users, list)
        or type(expected_count) is not int
        or expected_count < 0
    ):
        raise ProviderUnavailable("FOLLOWING_ENUMERATION_UNAVAILABLE")
    result = {}
    for user in users:
        if not isinstance(user, dict) or not NOTE_ID.fullmatch(
            str(user.get("user_id", ""))
        ):
            raise ProviderUnavailable("XHS_FOLLOWING_SCHEMA_CHANGED")
        uid = user["user_id"]
        result[uid] = {
            "author_id": uid,
            "nickname": str(user.get("nick_name", ""))[:300],
        }
    if data.get("has_more") not in {None, False} or len(result) != expected_count:
        raise ProviderUnavailable("XHS_FOLLOWING_COUNT_MISMATCH")
    return list(result.values())


class FollowingClient(XhsSessionClient):
    def following(self, owner):
        from playwright.sync_api import sync_playwright

        chrome = shutil.which("google-chrome")
        if not chrome:
            raise ProviderUnavailable("XHS_SOURCE_BROWSER_MISSING")
        root = self.video.root.parent / "xhs-session"
        responses = []

        def capture(response):
            parsed = urlsplit(response.url)
            if (
                parsed.hostname != "edith.xiaohongshu.com"
                or parsed.path != FOLLOWING_ALL
            ):
                return
            try:
                length = response.headers.get("content-length")
                if length and int(length) > 8_000_000:
                    return
                raw = response.body()
                if len(raw) > 8_000_000:
                    return
                import json

                body = json.loads(raw)
                if body.get("success") is True:
                    responses.append(body.get("data"))
            except Exception:  # noqa: BLE001 - redact browser and model details  # Browser response objects never escape into logs.
                return

        throttle_account(self.video)
        with browser_session_lock(root), sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(root / "profile"),
                executable_path=chrome,
                headless=True,
                locale="zh-CN",
                args=["--disable-dev-shm-usage"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.on("response", capture)
                page.goto(
                    "https://www.xiaohongshu.com/user/profile/" + owner,
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                page.wait_for_timeout(3000)
                counter = page.locator(".user-interactions").first
                if not counter.count():
                    raise ProviderUnavailable("FOLLOWING_ENUMERATION_UNAVAILABLE")
                text = counter.locator(":scope > div").first.inner_text()
                match = re.fullmatch(r"\s*([0-9,]+)\s*关注\s*", text)
                if not match:
                    raise ProviderUnavailable("XHS_FOLLOWING_COUNT_UNAVAILABLE")
                count = int(match[1].replace(",", ""))
                if not responses:
                    page.get_by_text("关注", exact=True).first.click(timeout=3000)
                    page.wait_for_timeout(3000)
                if not responses:
                    raise ProviderUnavailable("FOLLOWING_ENUMERATION_UNAVAILABLE")
                users = normalize_following(responses[-1], count)
                return {
                    "authors": users,
                    "complete": True,
                    "count": count,
                    "evidence": "following_all_matches_exact_profile_count",
                }
            except ProviderUnavailable:
                raise
            except Exception:  # noqa: BLE001 - redact browser and model details
                raise ProviderUnavailable("FOLLOWING_ENUMERATION_UNAVAILABLE") from None
            finally:
                context.close()

    def author_page(self, author_id, cursor):
        return self.get(
            "/api/sns/web/v1/user_posted",
            {
                "user_id": author_id,
                "num": 30,
                "cursor": cursor,
                "image_formats": "jpg,webp,avif",
                "xsec_source": "pc_user",
            },
        )
