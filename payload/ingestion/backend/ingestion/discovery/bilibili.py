from __future__ import annotations

import asyncio
import itertools
import re
from datetime import datetime, timezone
from typing import Any, Callable

import yt_dlp

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
from ..security import SafeYtdlpLogger, validate_public_url
from .errors import classify_platform_error
from .models import (
    AccessProbeResult,
    AuthorDiscoveryResult,
    AuthorSnapshot,
    DiscoveredWork,
    DiscoveryAdapterError,
)
from .url_classifier import classify_url, normalize_bilibili_author_url


BVID_RE = re.compile(r"^BV[A-Za-z0-9]{10}$", re.I)
AVID_RE = re.compile(r"^(?:av)?(\d{1,20})$", re.I)
RAW_ALLOWLIST = {
    "id",
    "title",
    "duration",
    "timestamp",
    "release_timestamp",
    "availability",
}


class BilibiliAuthorDiscoveryAdapter:
    platform = Platform.BILIBILI

    def __init__(
        self,
        settings: Settings,
        ydl_factory: Callable[[dict[str, Any]], Any] = yt_dlp.YoutubeDL,
    ):
        self.settings = settings
        self._ydl_factory = ydl_factory

    def classify_url(self, url: str) -> UrlKind:
        return classify_url(url).kind

    def normalize_author_url(self, url: str) -> str:
        return normalize_bilibili_author_url(url)

    def _options(self, scan_limit: int) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "lazy_playlist": True,
            "skip_download": True,
            "cachedir": False,
            "writesubtitles": False,
            "writeautomaticsub": False,
            "writethumbnail": False,
            "socket_timeout": 30,
            "http_headers": ytdlp_http_headers(),
            "logger": SafeYtdlpLogger(),
        }
        if scan_limit:
            options["playlistend"] = scan_limit
        if self.settings.bilibili_cookies_file:
            options["cookiefile"] = self.settings.bilibili_cookies_file
        return options

    async def discover(
        self, url: str, *, scan_limit: int
    ) -> AuthorDiscoveryResult:
        if scan_limit != 0 and not (
            1 <= scan_limit <= self.settings.author_discovery_scan_limit
        ):
            raise ValueError("scan_limit is outside the configured range")
        validate_public_url(url)
        canonical = self.normalize_author_url(url)
        with self._ydl_factory(self._options(scan_limit)) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, canonical, False)
        return self._parse(info, canonical, scan_limit)

    async def probe_access(self, author_url: str) -> AccessProbeResult:
        try:
            result = await self.discover(author_url, scan_limit=1)
        except Exception as exc:
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
                if result.author.author_id
                else AccessProbeStatus.UNAVAILABLE
            )
        )

    def _parse(
        self, info: dict[str, Any] | None, canonical: str, scan_limit: int
    ) -> AuthorDiscoveryResult:
        if not isinstance(info, dict) or info.get("_type") not in {
            "playlist",
            "multi_video",
        }:
            raise DiscoveryAdapterError(
                PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value,
                "Bilibili 作者页返回了不符合预期的数据结构",
            )
        canonical_mid = canonical.rstrip("/").split("/")[-2]
        author_id = next(
            (
                str(value)
                for key in ("uploader_id", "channel_id", "id")
                if (value := info.get(key))
                and str(value).isdigit()
                and str(value) != "0"
                and len(str(value)) <= 20
            ),
            canonical_mid if canonical_mid.isdigit() else None,
        )
        if not author_id or author_id != canonical_mid:
            raise DiscoveryAdapterError(
                PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value,
                "Bilibili 作者页缺少稳定作者 ID",
            )
        entries = info.get("entries")
        if entries is None:
            raise DiscoveryAdapterError(
                PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value,
                "Bilibili 作者页缺少作品列表",
            )

        works: list[DiscoveredWork] = []
        seen: set[str] = set()
        consumed = 0
        selected_entries = (
            entries if scan_limit == 0 else itertools.islice(entries, scan_limit)
        )
        for entry in selected_entries:
            consumed += 1
            if not isinstance(entry, dict):
                continue
            source_id = _source_id(entry)
            if source_id is None or source_id.lower() in seen:
                continue
            seen.add(source_id.lower())
            availability = str(entry.get("availability") or "").lower()
            eligibility = DiscoveryEligibility.ELIGIBLE
            if availability in {
                "private",
                "premium_only",
                "subscriber_only",
                "needs_auth",
            }:
                eligibility = DiscoveryEligibility.UNAVAILABLE
            duration = _duration(entry.get("duration"))
            if (
                duration is not None
                and duration > self.settings.max_video_duration_seconds
            ):
                eligibility = DiscoveryEligibility.UNSUPPORTED
            works.append(
                DiscoveredWork(
                    source_id=source_id,
                    canonical_url=f"https://www.bilibili.com/video/{source_id}",
                    title=_short_text(entry.get("title"), 500),
                    published_at=_timestamp(
                        entry.get("timestamp") or entry.get("release_timestamp")
                    ),
                    duration_seconds=duration,
                    content_type=WorkContentType.VIDEO,
                    position=len(works) + 1,
                    eligibility=eligibility,
                    raw_metadata={
                        key: entry[key]
                        for key in RAW_ALLOWLIST
                        if key in entry and _safe_scalar(entry[key])
                    },
                )
            )
        return AuthorDiscoveryResult(
            author=AuthorSnapshot(
                platform=Platform.BILIBILI,
                author_id=author_id,
                canonical_url=canonical,
                display_name=_short_text(
                    info.get("uploader") or info.get("channel") or info.get("title"),
                    300,
                ),
            ),
            works=tuple(works),
            extractor_name=str(
                info.get("extractor_key") or info.get("extractor") or "bilibili"
            ),
            truncated=scan_limit > 0 and consumed >= scan_limit,
        )


def _source_id(entry: dict[str, Any]) -> str | None:
    candidates = (
        entry.get("id"),
        entry.get("display_id"),
        entry.get("url"),
        entry.get("webpage_url"),
    )
    for value in candidates:
        text = str(value or "")
        match = re.search(r"(BV[A-Za-z0-9]{10}|av\d+)", text, re.I)
        if match and BVID_RE.fullmatch(match.group(1)):
            return "BV" + match.group(1)[2:]
        if match and (av_match := AVID_RE.fullmatch(match.group(1))):
            return f"av{av_match.group(1)}"
        if BVID_RE.fullmatch(text):
            return "BV" + text[2:]
        if av_match := AVID_RE.fullmatch(text):
            return f"av{av_match.group(1)}"
    return None


def _duration(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if 0 <= result <= 31_536_000 else None


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _short_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] or None


def _safe_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))
