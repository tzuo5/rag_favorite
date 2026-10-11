"""Shared owner-authenticated XHS signed reads; private account-wide pacing."""

from __future__ import annotations

import fcntl
import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.request

from .video_provider import ProviderUnavailable
from .xhs_private import (
    TrustedRedirect,
    atomic_private_json,
    private_directory,
    private_json,
    private_open,
)


def throttle_account(video, interval=3):
    root = video.root.parent / "xhs-session"
    private_directory(root)
    with private_open(root / "requests.lock", append=True) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / "requests.json"
        previous = private_json(path).get("at", 0) if path.exists() else 0
        delay = min(interval, max(0, interval - (time.time() - previous)))
        if delay:
            time.sleep(delay)
        atomic_private_json(path, {"at": time.time()})


NOTE_ID = re.compile(r"^[a-f0-9]{24}$")


class XhsSessionClient:
    def __init__(self, video):
        from xhshow import SessionManager, Xhshow

        path = video.root.parent / "xhs-session" / "cookies.txt"
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise ProviderUnavailable("XHS_LOGIN_REQUIRED")
        self.video = video
        self.jar = http.cookiejar.MozillaCookieJar(str(path))
        self.jar.load(ignore_discard=True, ignore_expires=False)
        self.cookies = {c.name: c.value for c in self.jar}
        if not all(self.cookies.get(k) for k in ("a1", "web_session")):
            raise ProviderUnavailable("XHS_LOGIN_REQUIRED")
        self.signer, self.session = Xhshow(), SessionManager()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            TrustedRedirect(lambda host: host == "edith.xiaohongshu.com"),
        )

    def get(self, uri, params=None):
        for attempt in range(3):
            try:
                return self._get(uri, params)
            except ProviderUnavailable as exc:
                if (
                    str(exc)
                    not in {"XHS_FAVORITES_REQUEST_FAILED", "XHS_FAVORITES_HTTP_ERROR"}
                    or attempt == 2
                ):
                    raise
                time.sleep(2 ** (attempt + 1))

    def _get(self, uri, params=None):
        throttle_account(self.video)
        headers = self.signer.sign_headers_get(
            uri, self.cookies, params=params, session=self.session
        )
        headers.update(
            {
                "Origin": "https://www.xiaohongshu.com",
                "Referer": "https://www.xiaohongshu.com/",
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            }
        )
        url = self.signer.build_url("https://edith.xiaohongshu.com" + uri, params or {})
        try:
            with self.opener.open(
                urllib.request.Request(url, headers=headers), timeout=30
            ) as response:
                raw = response.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise ProviderUnavailable("XHS_FAVORITES_RESPONSE_TOO_LARGE")
                body = json.loads(raw)
        except ProviderUnavailable:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise ProviderUnavailable(
                    "XHS_RATE_LIMITED", code="XHS_RATE_LIMITED"
                ) from None
            raise ProviderUnavailable(
                "XHS_LOGIN_OR_VERIFICATION_REQUIRED"
                if exc.code in {401, 403, 461, 471}
                else "XHS_FAVORITES_HTTP_ERROR"
            ) from None
        except Exception:  # noqa: BLE001 - redact private HTTP details
            raise ProviderUnavailable("XHS_FAVORITES_REQUEST_FAILED") from None
        if isinstance(body, dict) and body.get("code") in {-100, -101, 300012, 300013}:
            raise ProviderUnavailable("XHS_LOGIN_OR_VERIFICATION_REQUIRED")
        if not isinstance(body, dict) or body.get("success") is not True:
            raise ProviderUnavailable("XHS_FAVORITES_API_REJECTED")
        data = body.get("data")
        if not isinstance(data, dict):
            raise ProviderUnavailable("XHS_FAVORITES_SCHEMA_CHANGED")
        return data

    def owner(self):
        data = self.get("/api/sns/web/v2/user/me")
        owner = data.get("user_id") or data.get("userId")
        if (
            data.get("guest") is True
            or not isinstance(owner, str)
            or not NOTE_ID.fullmatch(owner)
        ):
            raise ProviderUnavailable("XHS_OWNER_ID_UNAVAILABLE")
        return owner

    def page(self, owner, cursor):
        return self.get(
            "/api/sns/web/v2/note/collect/page",
            {"user_id": owner, "num": 30, "cursor": cursor},
        )
