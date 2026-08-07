from __future__ import annotations

import asyncio
import http.cookiejar
import json
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from yt_dlp.utils import js_to_json

from backend.video_processor import ytdlp_http_headers

from ..config import Settings
from ..models import (
    AccessProbeStatus,
    DiscoveryEligibility,
    Platform,
    PlatformErrorCode,
    UrlKind,
    WorkContentType,
)
from ..security import validate_public_url
from .errors import classify_platform_error
from .models import (
    AccessProbeResult,
    AuthorDiscoveryResult,
    AuthorSnapshot,
    DiscoveredWork,
    DiscoveryAdapterError,
)
from .url_classifier import classify_url, normalize_xiaohongshu_author_url

NOTE_ID_RE = re.compile(r"^[0-9a-f]{16,64}$", re.IGNORECASE)
INITIAL_STATE_RE = re.compile(r"(?:window\.)?__INITIAL_STATE__\s*=\s*", re.IGNORECASE)
NEXT_DATA_RE = re.compile(
    r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
MAX_PROFILE_HTML_BYTES = 5 * 1024 * 1024
MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024
USER_POSTED_URI = "/api/sns/web/v1/user_posted"
USER_POSTED_URL = f"https://edith.xiaohongshu.com{USER_POSTED_URI}"
NOTE_FEED_URI = "/api/sns/web/v1/feed"
NOTE_FEED_URL = f"https://edith.xiaohongshu.com{NOTE_FEED_URI}"
API_PAGE_SIZE = 30

PageFetcher = Callable[[str, str, int], dict[str, Any]]


def cookie_auth_status(settings: Settings) -> str:
    """Return a secret-free readiness state for operator diagnostics."""
    return cookie_file_diagnostics(settings)["auth_status"]


def cookie_file_diagnostics(settings: Settings) -> dict[str, Any]:
    """Inspect the configured cookie file without returning its path or values."""
    cookie_file = (
        settings.xiaohongshu_cookies_file
        or settings.ytdlp_cookies_file
    )
    if not cookie_file:
        return {
            "auth_status": "missing",
            "file_status": "missing",
            "secure_permissions": False,
        }
    path = Path(cookie_file)
    file_status = _cookie_file_status(path)
    if file_status != "ready":
        return {
            "auth_status": (
                "missing" if file_status == "missing" else "incomplete"
            ),
            "file_status": file_status,
            "secure_permissions": False,
        }
    jar = _load_cookie_jar(cookie_file)
    cookies = _cookie_dict(jar)
    required = {
        name: bool(cookies.get(name))
        for name in ("a1", "web_session")
    }
    return {
        "auth_status": "ready" if all(required.values()) else "incomplete",
        "file_status": "ready",
        "secure_permissions": True,
        "required_cookie_count": sum(required.values()),
        "required_cookie_total": len(required),
    }


class XiaohongshuAuthorDiscoveryAdapter:
    """Profile discovery with signed cursor pagination when available.

    The server-rendered profile is retained as a no-login fallback.  An
    authenticated cookie jar plus ``xhshow`` enumerates the complete author
    feed without persisting cookies, signatures, or per-note ``xsec_token``
    values.
    """

    platform = Platform.XIAOHONGSHU

    def __init__(
        self,
        settings: Settings,
        html_fetcher: Callable[[str], str] | None = None,
        page_fetcher: PageFetcher | None = None,
    ):
        self.settings = settings
        self._cookie_file = (
            settings.xiaohongshu_cookies_file
            or settings.ytdlp_cookies_file
        )
        self._cookie_reload_lock = threading.RLock()
        self._cookie_fingerprint = _cookie_file_fingerprint(
            self._cookie_file
        )
        self._cookie_jar = _load_cookie_jar(self._cookie_file)
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar)
        )
        self._html_fetcher = html_fetcher or self._download_profile_html
        self._page_fetcher = page_fetcher
        self._signature_context: tuple[Any, Any] | None = None
        self._note_token_cache: dict[str, tuple[str, str, float]] = {}

    def classify_url(self, url: str) -> UrlKind:
        return classify_url(url).kind

    def normalize_author_url(self, url: str) -> str:
        return normalize_xiaohongshu_author_url(url)

    async def discover(
        self, url: str, *, scan_limit: int
    ) -> AuthorDiscoveryResult:
        self._reload_cookies_if_changed()
        if scan_limit != 0 and not (
            1 <= scan_limit <= self.settings.author_discovery_scan_limit
        ):
            raise ValueError("scan_limit is outside the configured range")
        validate_public_url(url)
        canonical = self.normalize_author_url(url)
        html = await asyncio.to_thread(self._html_fetcher, canonical)
        try:
            state = _extract_initial_state(html)
        except DiscoveryAdapterError as exc:
            has_authenticated_fallback = (
                self._page_fetcher is not None
                or _has_signing_cookie(self._cookie_jar)
            )
            if (
                exc.code
                != PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value
                or not has_authenticated_fallback
            ):
                raise
            # Risk-trimmed profile responses can omit the initialization
            # object entirely. An authenticated API request remains
            # authoritative and retains the normal captcha/risk-control
            # failure handling.
            state = {}
        return await self._parse_and_paginate(state, canonical, scan_limit)

    async def probe_access(self, author_url: str) -> AccessProbeResult:
        try:
            result = await self.discover(author_url, scan_limit=1)
        except Exception as exc:  # noqa: BLE001 - normalize adapter failures
            code = (
                PlatformErrorCode(exc.code)
                if isinstance(exc, DiscoveryAdapterError)
                else classify_platform_error(exc).code
            )
            status = {
                PlatformErrorCode.AUTH_EXPIRED: AccessProbeStatus.AUTH_REQUIRED,
                PlatformErrorCode.RATE_LIMITED: AccessProbeStatus.RATE_LIMITED,
                PlatformErrorCode.BOT_CHECK: AccessProbeStatus.BLOCKED,
                PlatformErrorCode.PLATFORM_BLOCKED: AccessProbeStatus.BLOCKED,
            }.get(code, AccessProbeStatus.UNAVAILABLE)
            return AccessProbeResult(status=status, error_code=code.value)
        return AccessProbeResult(
            status=(
                AccessProbeStatus.OK
                if result.author.author_id and result.works
                else AccessProbeStatus.UNAVAILABLE
            )
        )

    def _download_profile_html(self, canonical: str) -> str:
        headers = {
            key: str(value)
            for key, value in ytdlp_http_headers().items()
            if value is not None
        }
        headers["Accept-Encoding"] = "identity"
        request = urllib.request.Request(canonical, headers=headers)
        with self._opener.open(request, timeout=30) as response:
            payload = response.read(MAX_PROFILE_HTML_BYTES + 1)
            if len(payload) > MAX_PROFILE_HTML_BYTES:
                raise DiscoveryAdapterError(
                    PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value,
                    "小红书作者页响应超过安全上限",
                )
            charset = response.headers.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")

    async def _parse_and_paginate(
        self, state: Any, canonical: str, scan_limit: int
    ) -> AuthorDiscoveryResult:
        if not isinstance(state, (dict, list)):
            raise DiscoveryAdapterError(
                PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value,
                "小红书作者页缺少可识别的初始化数据",
            )
        author_id = canonical.rstrip("/").split("/")[-1]
        display_name = _author_name(state, author_id)
        works: list[DiscoveredWork] = []
        seen: set[str] = set()
        candidate_count = self._append_notes(
            _note_containers(state), works, seen, scan_limit
        )
        cursor = _page_cursor(state)
        has_more = _page_has_more(state)
        used_api = False

        # A risk-trimmed SSR state can contain an empty list and
        # ``has_more=false`` even though the authenticated API has posts.
        # Treat a completely empty first page as non-authoritative when a
        # signed page fetcher is available.
        if (
            scan_limit == 0
            or (
                len(works) < scan_limit
                and (not works or has_more is not False)
            )
        ):
            fetch_page = self._page_fetcher
            if fetch_page is None and _has_signing_cookie(self._cookie_jar):
                fetch_page = self._download_user_posted_page
            if scan_limit == 0 and fetch_page is not None:
                # The signed feed is authoritative for "all". Start from its
                # first page and deduplicate any SSR entries already collected.
                cursor = ""
                has_more = True
            visited_cursors: set[str] = set()
            while True:
                if (
                    fetch_page is None
                    or (scan_limit > 0 and len(works) >= scan_limit)
                ):
                    break
                page_size = (
                    API_PAGE_SIZE
                    if scan_limit == 0
                    else min(API_PAGE_SIZE, scan_limit - len(works))
                )
                payload = await asyncio.to_thread(
                    fetch_page, author_id, cursor or "", page_size
                )
                used_api = True
                page = _api_page(payload)
                notes = page.get("notes")
                if not isinstance(notes, list):
                    raise DiscoveryAdapterError(
                        PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value,
                        "小红书作者作品接口返回了不符合预期的数据结构",
                    )
                candidate_count += self._append_notes(
                    notes, works, seen, scan_limit
                )
                next_cursor = _clean_cursor(page.get("cursor"))
                page_has_more = _coerce_bool(page.get("has_more"))
                if (
                    page_has_more is False
                    or not notes
                    or not next_cursor
                    or next_cursor == cursor
                    or next_cursor in visited_cursors
                ):
                    has_more = bool(page_has_more)
                    break
                visited_cursors.add(next_cursor)
                cursor = next_cursor
                has_more = page_has_more
        if scan_limit == 0 and has_more is True:
            raise DiscoveryAdapterError(
                PlatformErrorCode.AUTH_EXPIRED.value,
                "小红书未能完整读取作者全部作品，需要有效登录 Cookie",
            )
        if not works:
            had_redacted_notes = any(
                isinstance(item.get("noteCard"), dict)
                and (
                    "noteId" in item["noteCard"]
                    or "type" in item["noteCard"]
                )
                for item in _walk_dicts(state)
            )
            missing_auth = (
                self._page_fetcher is None
                and not _has_signing_cookie(self._cookie_jar)
            )
            raise DiscoveryAdapterError(
                (
                    PlatformErrorCode.PLATFORM_BLOCKED.value
                    if had_redacted_notes
                    else PlatformErrorCode.AUTH_EXPIRED.value
                    if missing_auth
                    else PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value
                ),
                (
                    "小红书未向当前会话提供可用作品标识，已停止自动重试"
                    if had_redacted_notes
                    else "小红书作者分页需要有效登录 Cookie"
                    if missing_auth
                    else "小红书作者页未返回可识别的有限作品列表"
                ),
            )
        return AuthorDiscoveryResult(
            author=AuthorSnapshot(
                platform=Platform.XIAOHONGSHU,
                author_id=author_id,
                canonical_url=canonical,
                display_name=display_name,
            ),
            works=tuple(works),
            extractor_name=(
                "xiaohongshu-signed-user-posted"
                if used_api
                else "xiaohongshu-profile-initial-state"
            ),
            truncated=(
                scan_limit > 0
                and (
                    candidate_count > scan_limit
                    or len(works) >= scan_limit
                    or has_more is True
                )
            ),
        )

    def _append_notes(
        self,
        notes: Iterator[dict[str, Any]] | list[dict[str, Any]],
        works: list[DiscoveredWork],
        seen: set[str],
        scan_limit: int,
    ) -> int:
        added_candidates = 0
        for container in notes:
            if not isinstance(container, dict):
                continue
            note = (
                container.get("noteCard")
                or container.get("note_card")
                or container
            )
            if not isinstance(note, dict):
                continue
            source_id = _note_id(note)
            if source_id is None or source_id in seen:
                continue
            seen.add(source_id)
            added_candidates += 1
            if scan_limit > 0 and len(works) >= scan_limit:
                continue
            works.append(
                _discovered_work(
                    note,
                    source_id,
                    len(works) + 1,
                    self.settings.max_video_duration_seconds,
                )
            )
        return added_candidates

    def _download_user_posted_page(
        self, author_id: str, cursor: str, page_size: int
    ) -> dict[str, Any]:
        cookies = _cookie_dict(self._cookie_jar)
        if not cookies.get("a1") or not cookies.get("web_session"):
            raise DiscoveryAdapterError(
                PlatformErrorCode.AUTH_EXPIRED.value,
                "小红书作者分页需要有效登录 Cookie",
            )
        signer, session = self._signing_client()
        params: dict[str, str | int] = {
            "num": min(API_PAGE_SIZE, max(1, page_size)),
            "cursor": cursor,
            "user_id": author_id,
            "image_scenes": "FD_WM_WEBP",
        }
        signature = signer.sign_headers_get(
            USER_POSTED_URI,
            cookies,
            params=params,
            session=session,
        )
        url = signer.build_url(USER_POSTED_URL, params)
        return self._request_api_json(
            urllib.request.Request(
                url,
                headers=self._api_headers(author_id, signature),
            )
        )

    async def resolve_note_media(
        self,
        author_id: str,
        note_id: str,
        *,
        scan_limit: int = 50,
    ) -> dict[str, Any]:
        """Resolve a clean batch item to a transient signed CDN URL.

        The author listing is re-read just before processing so an expiring
        xsec token never needs to be stored in PostgreSQL.
        """
        self._reload_cookies_if_changed()
        if not NOTE_ID_RE.fullmatch(note_id):
            raise ValueError("invalid Xiaohongshu note ID")
        if scan_limit != 0 and not (
            1 <= scan_limit <= self.settings.author_discovery_scan_limit
        ):
            raise ValueError("scan_limit is outside the configured range")
        cached = self._note_token_cache.get(note_id)
        if (
            cached
            and cached[0] == author_id
            and cached[2] > time.monotonic()
        ):
            try:
                detail = await asyncio.to_thread(
                    self._download_note_detail,
                    author_id,
                    note_id,
                    cached[1],
                )
                return _resolved_note_media(detail, note_id, author_id)
            except DiscoveryAdapterError:
                self._note_token_cache.pop(note_id, None)
        cursor = ""
        visited: set[str] = set()
        scanned = 0
        while True:
            page_size = (
                API_PAGE_SIZE
                if scan_limit == 0
                else min(API_PAGE_SIZE, scan_limit - scanned)
            )
            if page_size <= 0:
                break
            payload = await asyncio.to_thread(
                self._download_user_posted_page,
                author_id,
                cursor,
                page_size,
            )
            page = _api_page(payload)
            notes = page.get("notes")
            if not isinstance(notes, list):
                raise DiscoveryAdapterError(
                    PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value,
                    "小红书作者作品接口返回了不符合预期的数据结构",
                )
            target_token: str | None = None
            for item in notes:
                if not isinstance(item, dict):
                    continue
                scanned += 1
                candidate = (
                    item.get("note_card")
                    or item.get("noteCard")
                    or item
                )
                if not isinstance(candidate, dict):
                    continue
                candidate_id = _note_id(candidate)
                token = (
                    item.get("xsec_token")
                    or item.get("xsecToken")
                    or candidate.get("xsec_token")
                    or candidate.get("xsecToken")
                )
                if candidate_id and token:
                    self._note_token_cache[candidate_id] = (
                        author_id,
                        str(token),
                        time.monotonic() + 1200,
                    )
                if candidate_id != note_id:
                    continue
                if not token:
                    raise DiscoveryAdapterError(
                        PlatformErrorCode.PLATFORM_BLOCKED.value,
                        "小红书未提供当前作品的临时访问令牌",
                    )
                target_token = str(token)
            if target_token:
                detail = await asyncio.to_thread(
                    self._download_note_detail,
                    author_id,
                    note_id,
                    target_token,
                )
                return _resolved_note_media(detail, note_id, author_id)
            next_cursor = _clean_cursor(page.get("cursor"))
            if (
                _coerce_bool(page.get("has_more")) is False
                or not notes
                or not next_cursor
                or next_cursor == cursor
                or next_cursor in visited
                or (scan_limit > 0 and scanned >= scan_limit)
            ):
                break
            visited.add(next_cursor)
            cursor = next_cursor
        raise DiscoveryAdapterError(
            PlatformErrorCode.DISCOVERY_FAILED.value,
            "小红书作者最近作品中未找到待处理笔记",
        )

    async def resolve_note_access_url(
        self,
        access_url: str,
        note_id: str,
    ) -> dict[str, Any]:
        """Consume a share token once and return only safe note context.

        The access URL is intentionally never cached or returned. The caller
        may persist the resolved author ID, then regenerate a fresh media
        capability from the author's bounded recent-work feed in the worker.
        """
        self._reload_cookies_if_changed()
        validate_public_url(access_url)
        parsed = urllib.parse.urlsplit(access_url)
        if (parsed.hostname or "").lower() not in {
            "xiaohongshu.com",
            "www.xiaohongshu.com",
        }:
            raise ValueError("not a Xiaohongshu note URL")
        if not NOTE_ID_RE.fullmatch(note_id):
            raise ValueError("invalid Xiaohongshu note ID")
        query = urllib.parse.parse_qs(parsed.query)
        token = next(iter(query.get("xsec_token") or []), "").strip()
        if not token or len(token) > 2000:
            raise DiscoveryAdapterError(
                PlatformErrorCode.AUTH_EXPIRED.value,
                "请从小红书分享按钮重新复制作品链接",
            )
        source = next(iter(query.get("xsec_source") or []), "pc_feed")
        payload = await asyncio.to_thread(
            self._download_note_detail,
            "",
            note_id,
            token,
            xsec_source=source,
            referer=access_url,
        )
        author_id = _note_author_id(payload, note_id)
        if not author_id:
            raise DiscoveryAdapterError(
                PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value,
                "小红书作品详情缺少作者标识",
            )
        return _resolved_note_media(payload, note_id, author_id)

    def _reload_cookies_if_changed(self) -> bool:
        current = _cookie_file_fingerprint(self._cookie_file)
        if current == self._cookie_fingerprint:
            return False
        with self._cookie_reload_lock:
            current = _cookie_file_fingerprint(self._cookie_file)
            if current == self._cookie_fingerprint:
                return False
            jar = _load_cookie_jar(self._cookie_file)
            self._cookie_jar = jar
            self._opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar)
            )
            self._signature_context = None
            self._note_token_cache.clear()
            self._cookie_fingerprint = current
            return True

    def _download_note_detail(
        self,
        author_id: str,
        note_id: str,
        xsec_token: str,
        *,
        xsec_source: str = "pc_user",
        referer: str | None = None,
    ) -> dict[str, Any]:
        cookies = _cookie_dict(self._cookie_jar)
        if not cookies.get("a1") or not cookies.get("web_session"):
            raise DiscoveryAdapterError(
                PlatformErrorCode.AUTH_EXPIRED.value,
                "小红书作品下载需要有效登录 Cookie",
            )
        payload = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": "1"},
            "xsec_source": (
                xsec_source
                if xsec_source in {"pc_feed", "pc_search", "pc_user"}
                else "pc_feed"
            ),
            "xsec_token": xsec_token,
        }
        signer, session = self._signing_client()
        signature = signer.sign_headers_post(
            NOTE_FEED_URI,
            cookies,
            payload=payload,
            session=session,
        )
        body = signer.build_json_body(payload).encode("utf-8")
        return self._request_api_json(
            urllib.request.Request(
                NOTE_FEED_URL,
                data=body,
                method="POST",
                headers={
                    **self._api_headers(
                        author_id,
                        signature,
                        referer=referer,
                    ),
                    "Content-Type": "application/json;charset=UTF-8",
                },
            )
        )

    def _signing_client(self) -> tuple[Any, Any]:
        if self._signature_context is not None:
            return self._signature_context
        try:
            from xhshow import CryptoConfig, SessionManager, Xhshow
        except ImportError as exc:
            raise DiscoveryAdapterError(
                PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value,
                "缺少小红书签名组件 xhshow",
            ) from exc
        user_agent = next(iter(ytdlp_http_headers().values()), "")
        config = CryptoConfig().with_overrides(
            PUBLIC_USERAGENT=str(user_agent)
        )
        self._signature_context = (Xhshow(config), SessionManager(config))
        return self._signature_context

    def _api_headers(
        self,
        author_id: str,
        signature: dict[str, str],
        *,
        referer: str | None = None,
    ) -> dict[str, str]:
        user_agent = next(iter(ytdlp_http_headers().values()), "")
        headers = {
            "User-Agent": str(user_agent),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://www.xiaohongshu.com",
            "Referer": referer or (
                f"https://www.xiaohongshu.com/user/profile/{author_id}"
            ),
            "Accept-Encoding": "identity",
            **signature,
        }
        return headers

    def _request_api_json(
        self, request: urllib.request.Request
    ) -> dict[str, Any]:
        try:
            with self._opener.open(request, timeout=30) as response:
                raw = response.read(MAX_API_RESPONSE_BYTES + 1)
                if len(raw) > MAX_API_RESPONSE_BYTES:
                    raise DiscoveryAdapterError(
                        PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value,
                        "小红书作者作品接口响应超过安全上限",
                    )
                charset = response.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as exc:
            raise _http_api_error(exc.code) from exc
        except urllib.error.URLError as exc:
            raise DiscoveryAdapterError(
                PlatformErrorCode.NETWORK_ERROR.value,
                "小红书网络访问失败",
            ) from exc
        try:
            payload = json.loads(raw.decode(charset, errors="replace"))
        except json.JSONDecodeError as exc:
            raise DiscoveryAdapterError(
                PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value,
                "小红书作者作品接口未返回 JSON",
            ) from exc
        return _validated_api_payload(payload)


def _load_cookie_jar(cookie_file: str | None) -> http.cookiejar.CookieJar:
    """Load Netscape or JSON browser cookies into an in-memory jar."""
    jar = http.cookiejar.MozillaCookieJar()
    if not cookie_file:
        return jar
    path = Path(cookie_file)
    if _cookie_file_status(path) != "ready":
        return jar
    try:
        jar.load(path, ignore_discard=True, ignore_expires=True)
        return jar
    except (http.cookiejar.LoadError, OSError):
        pass
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return jar
    entries: list[dict[str, Any]]
    if isinstance(raw, dict):
        if isinstance(raw.get("cookies"), list):
            entries = raw["cookies"]
        else:
            entries = [
                {"name": key, "value": value}
                for key, value in raw.items()
                if isinstance(value, (str, int, float))
            ]
    elif isinstance(raw, list):
        entries = [entry for entry in raw if isinstance(entry, dict)]
    else:
        return jar
    for entry in entries:
        name = str(entry.get("name") or "")
        value = str(entry.get("value") or "")
        if not name or not value:
            continue
        domain = str(entry.get("domain") or ".xiaohongshu.com")
        if not _is_xiaohongshu_cookie_domain(domain):
            continue
        cookie = http.cookiejar.Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=domain.startswith("."),
            path=str(entry.get("path") or "/"),
            path_specified=True,
            secure=bool(entry.get("secure", True)),
            expires=_cookie_expiry(entry),
            discard=False,
            comment=None,
            comment_url=None,
            rest={"HttpOnly": entry.get("httpOnly", False)},
            rfc2109=False,
        )
        jar.set_cookie(cookie)
    return jar


def _cookie_file_fingerprint(
    cookie_file: str | None,
) -> tuple[int, int, int, int] | None:
    if not cookie_file:
        return None
    try:
        metadata = Path(cookie_file).stat()
    except OSError:
        return None
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mtime_ns,
        metadata.st_size,
    )


def _cookie_expiry(entry: dict[str, Any]) -> int | None:
    value = entry.get("expirationDate") or entry.get("expires")
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _cookie_dict(jar: http.cookiejar.CookieJar) -> dict[str, str]:
    return {
        cookie.name: cookie.value
        for cookie in jar
        if (
            cookie.value
            and not cookie.is_expired()
            and _is_xiaohongshu_cookie_domain(cookie.domain)
        )
    }


def _is_xiaohongshu_cookie_domain(domain: str) -> bool:
    normalized = domain.lstrip(".").lower()
    return (
        normalized == "xiaohongshu.com"
        or normalized.endswith(".xiaohongshu.com")
    )


def _cookie_file_status(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError:
        return "missing"
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        return "insecure_file_type"
    if metadata.st_uid != os.geteuid():
        return "wrong_owner"
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        return "insecure_permissions"
    if not metadata.st_mode & stat.S_IRUSR or not os.access(path, os.R_OK):
        return "unreadable"
    return "ready"


def _has_signing_cookie(jar: http.cookiejar.CookieJar) -> bool:
    cookies = _cookie_dict(jar)
    return bool(cookies.get("a1") and cookies.get("web_session"))


def _http_api_error(status: int) -> DiscoveryAdapterError:
    if status in {401}:
        return DiscoveryAdapterError(
            PlatformErrorCode.AUTH_EXPIRED.value,
            "小红书登录状态已失效",
        )
    if status in {429}:
        return DiscoveryAdapterError(
            PlatformErrorCode.RATE_LIMITED.value,
            "小红书请求频率受限",
        )
    if status in {461, 471}:
        return DiscoveryAdapterError(
            PlatformErrorCode.BOT_CHECK.value,
            "小红书要求人机验证",
        )
    if status in {403, 412}:
        return DiscoveryAdapterError(
            PlatformErrorCode.PLATFORM_BLOCKED.value,
            "小红书暂时阻止了作者作品访问",
        )
    if 500 <= status <= 599:
        return DiscoveryAdapterError(
            PlatformErrorCode.PLATFORM_UNAVAILABLE.value,
            "小红书服务暂时不可用",
        )
    return DiscoveryAdapterError(
        PlatformErrorCode.DISCOVERY_FAILED.value,
        "无法读取小红书作者作品列表",
    )


def _validated_api_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DiscoveryAdapterError(
            PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value,
            "小红书作者作品接口返回了不符合预期的数据结构",
        )
    if payload.get("success") is True:
        data = payload.get("data")
        if isinstance(data, dict):
            return data
    code = payload.get("code")
    if code == -100:
        raise DiscoveryAdapterError(
            PlatformErrorCode.AUTH_EXPIRED.value,
            "小红书登录状态已失效",
        )
    if code in {300012, 300013}:
        raise DiscoveryAdapterError(
            PlatformErrorCode.PLATFORM_BLOCKED.value,
            "小红书风控暂时阻止了作者作品访问",
        )
    if code == 300015:
        raise DiscoveryAdapterError(
            PlatformErrorCode.PLATFORM_BLOCKED.value,
            "小红书签名校验未通过",
        )
    raise DiscoveryAdapterError(
        PlatformErrorCode.DISCOVERY_FAILED.value,
        "小红书作者作品接口请求失败",
    )


def _api_page(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DiscoveryAdapterError(
            PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value,
            "小红书作者作品接口返回了不符合预期的数据结构",
        )
    if "success" in payload or "code" in payload:
        payload = _validated_api_payload(payload)
    if not isinstance(payload, dict):
        raise DiscoveryAdapterError(
            PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value,
            "小红书作者作品接口缺少分页数据",
        )
    return payload


def _note_containers(state: Any) -> Iterator[dict[str, Any]]:
    for container in _walk_dicts(state):
        note = (
            container.get("noteCard")
            or container.get("note_card")
            or container
        )
        if isinstance(note, dict) and _note_id(note):
            yield container


def _page_cursor(value: Any) -> str | None:
    priority_keys = {
        "cursor",
        "nextCursor",
        "next_cursor",
        "cursorScore",
        "cursor_score",
    }
    for item in _walk_dicts(value):
        if not (
            isinstance(item.get("notes"), list)
            or isinstance(item.get("items"), list)
            or isinstance(item.get("noteList"), list)
            or isinstance(item.get("note_list"), list)
        ):
            continue
        for key in priority_keys:
            cursor = _clean_cursor(item.get(key))
            if cursor:
                return cursor
    return None


def _page_has_more(value: Any) -> bool | None:
    for item in _walk_dicts(value):
        if not (
            isinstance(item.get("notes"), list)
            or isinstance(item.get("items"), list)
            or isinstance(item.get("noteList"), list)
            or isinstance(item.get("note_list"), list)
        ):
            continue
        for key in ("hasMore", "has_more"):
            if key in item:
                return _coerce_bool(item.get(key))
    return None


def _clean_cursor(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > 500:
        return None
    return text


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in {0, "0", "false", "False"}:
        return False
    if value in {1, "1", "true", "True"}:
        return True
    return None


def _discovered_work(
    note: dict[str, Any],
    source_id: str,
    position: int,
    max_duration_seconds: int,
) -> DiscoveredWork:
    note_type = str(
        note.get("type")
        or note.get("noteType")
        or note.get("note_type")
        or ""
    ).lower()
    is_video = (
        note_type in {"video", "normal_video"}
        or isinstance(note.get("video"), dict)
        or isinstance(note.get("videoInfo"), dict)
        or isinstance(note.get("video_info"), dict)
    )
    duration = _duration_seconds(note)
    eligibility = (
        DiscoveryEligibility.ELIGIBLE
        if is_video
        else DiscoveryEligibility.UNSUPPORTED
    )
    if duration is not None and duration > max_duration_seconds:
        eligibility = DiscoveryEligibility.UNSUPPORTED
    title = _short_text(
        note.get("displayTitle")
        or note.get("display_title")
        or note.get("title")
        or note.get("desc"),
        500,
    )
    return DiscoveredWork(
        source_id=source_id,
        canonical_url=f"https://www.xiaohongshu.com/explore/{source_id}",
        title=title,
        published_at=_published_at(note) or _published_at_from_note_id(source_id),
        duration_seconds=duration,
        content_type=(
            WorkContentType.VIDEO if is_video else WorkContentType.UNKNOWN
        ),
        position=position,
        eligibility=eligibility,
        # xsec_token is intentionally consumed only inside a request and never
        # enters discovery results, database rows, notifications, or logs.
        raw_metadata={
            "id": source_id,
            "type": note_type or None,
            "title": title,
            "duration": duration,
        },
    )


def _resolved_note_media(
    payload: dict[str, Any], note_id: str, author_id: str
) -> dict[str, Any]:
    items = payload.get("items")
    if not isinstance(items, list):
        raise DiscoveryAdapterError(
            PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value,
            "小红书作品详情接口缺少作品列表",
        )
    note: dict[str, Any] | None = None
    for item in items:
        if not isinstance(item, dict):
            continue
        candidate = (
            item.get("note_card")
            or item.get("noteCard")
            or item
        )
        if not isinstance(candidate, dict):
            continue
        candidate_id = _note_id(candidate) or _note_id(item)
        if candidate_id in {None, note_id}:
            note = candidate
            break
    if note is None:
        raise DiscoveryAdapterError(
            PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value,
            "小红书作品详情接口未返回目标作品",
        )
    if str(note.get("type") or "").lower() not in {
        "video",
        "normal_video",
    } and not isinstance(note.get("video"), dict):
        raise DiscoveryAdapterError(
            PlatformErrorCode.DISCOVERY_FAILED.value,
            "该小红书笔记不是可下载的视频",
        )
    media_url = _best_video_url(note)
    if not media_url:
        raise DiscoveryAdapterError(
            PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value,
            "小红书作品详情缺少可下载的视频流",
        )
    user = note.get("user")
    if not isinstance(user, dict):
        user = {}
    resolved_author_id = str(
        user.get("user_id") or user.get("userId") or author_id
    )
    if resolved_author_id != author_id:
        raise DiscoveryAdapterError(
            PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value,
            "小红书作品作者与批次作者不一致",
        )
    tags = [
        _short_text(tag.get("name"), 100)
        for tag in (note.get("tag_list") or note.get("tagList") or [])
        if isinstance(tag, dict) and tag.get("name")
    ]
    return {
        "id": note_id,
        "title": _short_text(
            note.get("title") or note.get("display_title"), 500
        ),
        "description": _short_text(note.get("desc"), 4000),
        "author": _short_text(
            user.get("nickname") or user.get("nickName"), 300
        ),
        "author_id": resolved_author_id,
        "duration": _video_duration_seconds(note),
        "tags": [tag for tag in tags if tag],
        "thumbnail_url": _best_cover_url(note),
        # This signed CDN URL is deliberately returned only to the active
        # worker. Callers must not place it in metadata, logs, or SQL.
        "media_url": media_url,
    }


def _note_author_id(payload: dict[str, Any], note_id: str) -> str | None:
    items = payload.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        note = item.get("note_card") or item.get("noteCard") or item
        if not isinstance(note, dict):
            continue
        candidate_id = _note_id(note) or _note_id(item)
        if candidate_id not in {None, note_id}:
            continue
        user = note.get("user")
        if isinstance(user, dict):
            author_id = user.get("user_id") or user.get("userId")
            if author_id:
                return str(author_id)
    return None


def _best_cover_url(note: dict[str, Any]) -> str | None:
    containers: list[Any] = [
        note.get("cover"),
        note.get("image_list"),
        note.get("imageList"),
    ]
    video = note.get("video")
    if isinstance(video, dict):
        containers.extend([
            video.get("cover"),
            video.get("first_frame"),
            video.get("firstFrame"),
        ])
    choices: list[tuple[float, str]] = []
    for container in containers:
        for item in _walk_dicts(container):
            score = _numeric(
                item.get("width") or item.get("image_width") or 0
            ) * _numeric(
                item.get("height") or item.get("image_height") or 0
            )
            values = [
                item.get("url"),
                item.get("url_default"),
                item.get("urlDefault"),
                item.get("master_url"),
                item.get("masterUrl"),
            ]
            info_list = item.get("info_list") or item.get("infoList")
            if isinstance(info_list, list):
                values.extend(
                    info.get("url")
                    for info in info_list
                    if isinstance(info, dict)
                )
            for value in values:
                url = _safe_xhs_media_url(value)
                if url:
                    choices.append((score, url))
    if not choices:
        return None
    return max(choices, key=lambda choice: choice[0])[1]


def _best_video_url(note: dict[str, Any]) -> str | None:
    video = note.get("video")
    if not isinstance(video, dict):
        video = note.get("video_info") or note.get("videoInfo")
    if not isinstance(video, dict):
        return None
    choices: list[tuple[float, str]] = []
    for item in _walk_dicts(video):
        score = _numeric(
            item.get("avg_bitrate")
            or item.get("avgBitrate")
            or item.get("video_bitrate")
            or item.get("videoBitrate")
            or 0
        )
        values: list[Any] = [
            item.get("master_url"),
            item.get("masterUrl"),
            item.get("url"),
        ]
        for key in ("backup_urls", "backupUrls"):
            backup = item.get(key)
            if isinstance(backup, list):
                values.extend(backup)
        for value in values:
            url = _safe_xhs_media_url(value)
            if url:
                choices.append((score, url))
    if not choices:
        return None
    return max(choices, key=lambda choice: choice[0])[1]


def _safe_xhs_media_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 5000:
        return None
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or not (
            host.endswith(".xhscdn.com")
            or host == "xhscdn.com"
        )
    ):
        return None
    # The signed feed currently returns HTTP URLs for some video streams even
    # though the same trusted CDN endpoint supports TLS. Never pass the
    # downgrade through to the downloader; retain the signed path/query while
    # upgrading the scheme.
    return urllib.parse.urlunsplit((
        "https",
        parsed.netloc,
        parsed.path,
        parsed.query,
        "",
    ))


def _numeric(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0
    return max(result, 0)


def _video_duration_seconds(note: dict[str, Any]) -> float | None:
    direct = _duration_seconds(note)
    if direct is not None:
        return direct
    video = note.get("video")
    if not isinstance(video, dict):
        return None
    durations = [
        duration
        for item in _walk_dicts(video)
        if (duration := _duration_seconds(item)) is not None
    ]
    return max(durations) if durations else None


def _extract_initial_state(html: str) -> Any:
    match = INITIAL_STATE_RE.search(html)
    if match:
        literal = _balanced_object(html, match.end())
        if literal:
            try:
                return json.loads(js_to_json(literal))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
    next_data = NEXT_DATA_RE.search(html)
    if next_data:
        try:
            return json.loads(next_data.group(1))
        except json.JSONDecodeError:
            pass
    raise DiscoveryAdapterError(
        PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value,
        "小红书作者页初始化数据格式已变化",
    )


def _balanced_object(text: str, start: int) -> str | None:
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] not in "[{":
        return None
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'", "`"}:
            quote = char
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _walk_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _note_id(note: dict[str, Any]) -> str | None:
    explicit = note.get("noteId") or note.get("note_id")
    if explicit and NOTE_ID_RE.fullmatch(str(explicit)):
        return str(explicit).lower()
    candidate = note.get("id")
    note_markers = {
        "displayTitle",
        "display_title",
        "noteType",
        "note_type",
        "interactInfo",
        "interact_info",
        "cover",
        "video",
        "videoInfo",
        "video_info",
        "xsecToken",
        "xsec_token",
    }
    if (
        candidate
        and NOTE_ID_RE.fullmatch(str(candidate))
        and note_markers.intersection(note)
    ):
        return str(candidate).lower()
    return None


def _author_name(state: Any, author_id: str) -> str | None:
    fallback = None
    for item in _walk_dicts(state):
        item_id = item.get("userId") or item.get("user_id") or item.get("id")
        name = item.get("nickname") or item.get("nickName") or item.get("name")
        if name and fallback is None and (
            "userId" in item or "nickname" in item or "nickName" in item
        ):
            fallback = _short_text(name, 300)
        if str(item_id or "") == author_id and name:
            return _short_text(name, 300)
    return fallback


def _published_at(note: dict[str, Any]) -> datetime | None:
    value = (
        note.get("time")
        or note.get("timestamp")
        or note.get("publishTime")
        or note.get("publish_time")
        or note.get("lastUpdateTime")
        or note.get("last_update_time")
    )
    if value is None:
        return None
    try:
        timestamp = float(value)
        if timestamp > 100_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _published_at_from_note_id(source_id: str) -> datetime | None:
    """XHS note IDs retain the creation epoch in their first four bytes."""
    if len(source_id) < 8:
        return None
    try:
        timestamp = int(source_id[:8], 16)
        result = datetime.fromtimestamp(timestamp, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    now = datetime.now(timezone.utc)
    if datetime(2013, 1, 1, tzinfo=timezone.utc) <= result <= now:
        return result
    return None


def _duration_seconds(note: dict[str, Any]) -> float | None:
    value = note.get("duration") or note.get("duration_ms")
    video = (
        note.get("video")
        or note.get("videoInfo")
        or note.get("video_info")
    )
    if value is None and isinstance(video, dict):
        value = video.get("duration") or video.get("duration_ms")
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    # Xiaohongshu initial-state video durations are milliseconds.
    if result > 1_000:
        result /= 1000
    return result if 0 <= result <= 31_536_000 else None


def _short_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] or None
