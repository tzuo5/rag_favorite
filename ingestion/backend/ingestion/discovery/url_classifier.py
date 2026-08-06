from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from ..models import Platform, UrlKind
from ..security import validate_public_url

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
YOUTUBE_AUTHOR_PREFIXES = {"channel", "c", "user"}
YOUTUBE_UNSUPPORTED_ROOTS = {
    "playlist",
    "results",
    "feed",
    "hashtag",
    "gaming",
    "podcasts",
}
YOUTUBE_UNSUPPORTED_AUTHOR_TABS = {
    "shorts",
    "streams",
    "playlists",
    "community",
    "channels",
    "about",
    "featured",
}
BILIBILI_HOSTS = {
    "bilibili.com",
    "www.bilibili.com",
    "m.bilibili.com",
    "space.bilibili.com",
}
BILIBILI_SHORT_HOSTS = {
    "b23.tv",
    "www.b23.tv",
}
BILIBILI_UNSUPPORTED_ROOTS = {
    "bangumi",
    "cheese",
    "list",
    "medialist",
    "v",
}
BILIBILI_AUTHOR_UNSUPPORTED_TABS = {
    "article",
    "audio",
    "channel",
    "dynamic",
    "favlist",
    "search",
}
BILIBILI_VIDEO_ID_RE = re.compile(
    r"^(?:BV[A-Za-z0-9]{10}|av\d+)$",
    re.IGNORECASE,
)
XIAOHONGSHU_HOSTS = {
    "xiaohongshu.com",
    "www.xiaohongshu.com",
}
XIAOHONGSHU_SHORT_HOSTS = {
    "xhslink.com",
    "www.xhslink.com",
    "xhslink.cn",
    "www.xhslink.cn",
}
XIAOHONGSHU_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
XIAOHONGSHU_NOTE_ID_RE = re.compile(
    r"^[0-9a-f]{16,64}$",
    re.IGNORECASE,
)
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
SHORT_LINK_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
    "Chrome/125.0 Mobile Safari/537.36"
)


@dataclass(frozen=True)
class ClassifiedUrl:
    kind: UrlKind
    platform: Platform
    normalized_url: str | None = None
    # A share URL may carry a short-lived xsec_token required to resolve the
    # author safely. It is consumed during enqueue and must never be persisted,
    # printed, or logged.
    transient_access_url: str | None = None


class ShortLinkResolutionError(ValueError):
    """A recognized share link could not be expanded to a supported URL."""

    def __init__(
        self,
        message: str,
        platform: Platform = Platform.XIAOHONGSHU,
    ):
        super().__init__(message)
        self.platform = platform


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def normalize_youtube_author_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    if host not in YOUTUBE_HOSTS or host == "youtu.be":
        raise ValueError("not a supported YouTube author URL")
    parts = _segments(parsed.path)
    if parts and parts[-1].lower() == "videos":
        parts.pop()
    valid = (
        len(parts) == 1
        and parts[0].startswith("@")
        and len(parts[0]) > 1
    ) or (
        len(parts) == 2
        and parts[0].lower() in YOUTUBE_AUTHOR_PREFIXES
        and bool(parts[1])
    )
    if not valid:
        raise ValueError("not a supported YouTube author URL")
    path = "/" + "/".join(parts) + "/videos"
    return urlunsplit(("https", "www.youtube.com", path, "", ""))


def normalize_bilibili_author_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    parts = _segments(parsed.path)
    if host == "space.bilibili.com":
        author_parts = parts
    elif host == "m.bilibili.com" and parts[:1] == ["space"]:
        author_parts = parts[1:]
    else:
        raise ValueError("not a supported Bilibili author URL")
    if (
        not author_parts
        or not author_parts[0].isdigit()
        or len(author_parts[0]) > 20
        or author_parts[0] == "0"
    ):
        raise ValueError("not a supported Bilibili author URL")
    suffix = [part.lower() for part in author_parts[1:]]
    if suffix not in ([], ["video"], ["upload", "video"]):
        raise ValueError("not a supported Bilibili author URL")
    return f"https://space.bilibili.com/{author_parts[0]}/video"


def normalize_xiaohongshu_author_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    parts = _segments(parsed.path)
    if (
        host not in XIAOHONGSHU_HOSTS
        or len(parts) != 3
        or [part.lower() for part in parts[:2]] != ["user", "profile"]
        or not XIAOHONGSHU_ID_RE.fullmatch(parts[2])
    ):
        raise ValueError("not a supported Xiaohongshu author URL")
    # Query parameters can contain short-lived xsec_token values. They must not
    # become canonical identifiers or enter the discovery database.
    return f"https://www.xiaohongshu.com/user/profile/{parts[2]}"


def normalize_author_url(url: str) -> str:
    """Backward-compatible YouTube normalizer."""
    return normalize_youtube_author_url(url)


def _classify_youtube(parsed) -> ClassifiedUrl:
    host = parsed.hostname.rstrip(".").lower()
    parts = _segments(parsed.path)

    if host == "youtu.be":
        kind = UrlKind.SINGLE_WORK if len(parts) == 1 and parts[0] else UrlKind.UNKNOWN
        return ClassifiedUrl(kind, Platform.YOUTUBE)
    if (
        (parts[:1] == ["watch"] and bool(parse_qs(parsed.query).get("v")))
        or (len(parts) == 2 and parts[0].lower() in {"shorts", "live"} and parts[1])
    ):
        return ClassifiedUrl(UrlKind.SINGLE_WORK, Platform.YOUTUBE)
    if parts and parts[0].lower() in YOUTUBE_UNSUPPORTED_ROOTS:
        return ClassifiedUrl(UrlKind.UNSUPPORTED_COLLECTION, Platform.YOUTUBE)

    is_handle = len(parts) in {1, 2} and parts[0].startswith("@")
    is_legacy = (
        len(parts) in {2, 3}
        and parts[0].lower() in YOUTUBE_AUTHOR_PREFIXES
        and bool(parts[1])
    )
    if is_handle or is_legacy:
        suffix = parts[-1].lower()
        base_length = 1 if is_handle else 2
        if len(parts) == base_length or suffix == "videos":
            try:
                normalized = normalize_youtube_author_url(
                    urlunsplit(parsed)
                )
            except ValueError:
                return ClassifiedUrl(UrlKind.UNKNOWN, Platform.YOUTUBE)
            return ClassifiedUrl(UrlKind.AUTHOR_PAGE, Platform.YOUTUBE, normalized)
        if suffix in YOUTUBE_UNSUPPORTED_AUTHOR_TABS:
            return ClassifiedUrl(UrlKind.UNSUPPORTED_COLLECTION, Platform.YOUTUBE)
    return ClassifiedUrl(UrlKind.UNKNOWN, Platform.YOUTUBE)


def _classify_bilibili(parsed) -> ClassifiedUrl:
    host = parsed.hostname.rstrip(".").lower()
    parts = _segments(parsed.path)
    if host == "space.bilibili.com" or (
        host == "m.bilibili.com" and parts[:1] == ["space"]
    ):
        author_parts = parts if host == "space.bilibili.com" else parts[1:]
        if author_parts and author_parts[0].isdigit():
            suffix = [part.lower() for part in author_parts[1:]]
            if suffix in ([], ["video"], ["upload", "video"]):
                try:
                    normalized = normalize_bilibili_author_url(urlunsplit(parsed))
                except ValueError:
                    return ClassifiedUrl(UrlKind.UNKNOWN, Platform.BILIBILI)
                return ClassifiedUrl(
                    UrlKind.AUTHOR_PAGE, Platform.BILIBILI, normalized
                )
            if suffix and suffix[0] in BILIBILI_AUTHOR_UNSUPPORTED_TABS:
                return ClassifiedUrl(
                    UrlKind.UNSUPPORTED_COLLECTION, Platform.BILIBILI
                )
        return ClassifiedUrl(UrlKind.UNKNOWN, Platform.BILIBILI)
    if (
        len(parts) == 2
        and parts[0].lower() == "video"
        and BILIBILI_VIDEO_ID_RE.fullmatch(parts[1])
    ):
        source_id = (
            "BV" + parts[1][2:]
            if parts[1].lower().startswith("bv")
            else "av" + parts[1][2:]
        )
        return ClassifiedUrl(
            UrlKind.SINGLE_WORK,
            Platform.BILIBILI,
            f"https://www.bilibili.com/video/{source_id}",
        )
    if parts and parts[0].lower() in BILIBILI_UNSUPPORTED_ROOTS:
        return ClassifiedUrl(UrlKind.UNSUPPORTED_COLLECTION, Platform.BILIBILI)
    return ClassifiedUrl(UrlKind.UNKNOWN, Platform.BILIBILI)


def _classify_xiaohongshu(parsed) -> ClassifiedUrl:
    parts = _segments(parsed.path)
    lowered = [part.lower() for part in parts]
    if (
        len(parts) == 2
        and lowered[0] in {"explore", "discovery"}
        and (
            lowered[0] == "explore"
            or (lowered[0] == "discovery" and lowered[1] == "item")
        )
    ):
        note_id = parts[1] if lowered[0] == "explore" else ""
        if note_id and XIAOHONGSHU_NOTE_ID_RE.fullmatch(note_id):
            return ClassifiedUrl(
                UrlKind.SINGLE_WORK,
                Platform.XIAOHONGSHU,
                f"https://www.xiaohongshu.com/explore/{note_id.lower()}",
                urlunsplit(parsed) if parse_qs(parsed.query).get("xsec_token") else None,
            )
    if (
        len(parts) == 3
        and lowered[:2] == ["discovery", "item"]
        and XIAOHONGSHU_NOTE_ID_RE.fullmatch(parts[2])
    ):
        return ClassifiedUrl(
            UrlKind.SINGLE_WORK,
            Platform.XIAOHONGSHU,
            f"https://www.xiaohongshu.com/explore/{parts[2].lower()}",
            urlunsplit(parsed) if parse_qs(parsed.query).get("xsec_token") else None,
        )
    if len(parts) >= 3 and lowered[:2] == ["user", "profile"]:
        if len(parts) == 3 and XIAOHONGSHU_ID_RE.fullmatch(parts[2]):
            try:
                normalized = normalize_xiaohongshu_author_url(urlunsplit(parsed))
            except ValueError:
                return ClassifiedUrl(UrlKind.UNKNOWN, Platform.XIAOHONGSHU)
            return ClassifiedUrl(
                UrlKind.AUTHOR_PAGE, Platform.XIAOHONGSHU, normalized
            )
        return ClassifiedUrl(
            UrlKind.UNSUPPORTED_COLLECTION, Platform.XIAOHONGSHU
        )
    if lowered[:1] in (["search_result"], ["explore"]):
        return ClassifiedUrl(
            UrlKind.UNSUPPORTED_COLLECTION, Platform.XIAOHONGSHU
        )
    return ClassifiedUrl(UrlKind.UNKNOWN, Platform.XIAOHONGSHU)


def classify_url(url: str) -> ClassifiedUrl:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ClassifiedUrl(UrlKind.UNKNOWN, Platform.OTHER)
    host = parsed.hostname.rstrip(".").lower()
    if host in YOUTUBE_HOSTS:
        return _classify_youtube(parsed)
    if host in BILIBILI_HOSTS:
        return _classify_bilibili(parsed)
    if host in BILIBILI_SHORT_HOSTS:
        return ClassifiedUrl(UrlKind.UNKNOWN, Platform.BILIBILI)
    if host in XIAOHONGSHU_HOSTS:
        return _classify_xiaohongshu(parsed)
    if host in XIAOHONGSHU_SHORT_HOSTS:
        return ClassifiedUrl(UrlKind.UNKNOWN, Platform.XIAOHONGSHU)
    return ClassifiedUrl(UrlKind.UNKNOWN, Platform.OTHER)


def classify_input_url(
    url: str,
    *,
    opener=None,
    timeout: float = 5.0,
) -> ClassifiedUrl:
    """Classify user input, expanding a trusted platform share redirect.

    The redirect is never followed automatically. Both the input and redirect
    target pass the public-URL guard, and only a target from the expected
    platform is accepted. Returned identifiers are query-free canonical URLs.
    """
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    if host in BILIBILI_SHORT_HOSTS:
        return _resolve_bilibili_short_link(
            url,
            opener=opener,
            timeout=timeout,
        )
    if host not in XIAOHONGSHU_SHORT_HOSTS:
        return classify_url(url)

    validate_public_url(url)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": SHORT_LINK_USER_AGENT},
        method="GET",
    )
    client = opener or urllib.request.build_opener(_NoRedirectHandler())
    response = None
    try:
        try:
            response = client.open(request, timeout=timeout)
            status = int(getattr(response, "status", 0) or 0)
            location = response.headers.get("Location")
        except urllib.error.HTTPError as exc:
            response = exc
            status = int(exc.code)
            location = exc.headers.get("Location")
        if status not in REDIRECT_STATUSES or not location:
            raise ShortLinkResolutionError(
                "Xiaohongshu share link did not return a redirect"
            )
        target = urljoin(url, location)
        validate_public_url(target)
        classified = classify_url(target)
        if (
            classified.platform != Platform.XIAOHONGSHU
            or classified.kind == UrlKind.UNKNOWN
        ):
            raise ShortLinkResolutionError(
                "Xiaohongshu share link target is unsupported"
            )
        return ClassifiedUrl(
            classified.kind,
            classified.platform,
            classified.normalized_url,
            target if classified.kind == UrlKind.SINGLE_WORK else None,
        )
    except ShortLinkResolutionError:
        raise
    except Exception as exc:
        raise ShortLinkResolutionError(
            "Xiaohongshu share link could not be resolved"
        ) from exc
    finally:
        if response is not None:
            response.close()


def _resolve_bilibili_short_link(
    url: str,
    *,
    opener=None,
    timeout: float,
) -> ClassifiedUrl:
    """Expand one b23.tv redirect and accept only a Bilibili work/author URL."""
    validate_public_url(url)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": SHORT_LINK_USER_AGENT},
        method="GET",
    )
    client = opener or urllib.request.build_opener(_NoRedirectHandler())
    response = None
    try:
        try:
            response = client.open(request, timeout=timeout)
            status = int(getattr(response, "status", 0) or 0)
            location = response.headers.get("Location")
        except urllib.error.HTTPError as exc:
            response = exc
            status = int(exc.code)
            location = exc.headers.get("Location")
        if status not in REDIRECT_STATUSES or not location:
            raise ShortLinkResolutionError(
                "Bilibili share link did not return a redirect",
                Platform.BILIBILI,
            )
        target = urljoin(url, location)
        validate_public_url(target)
        classified = classify_url(target)
        if (
            classified.platform != Platform.BILIBILI
            or classified.kind == UrlKind.UNKNOWN
        ):
            raise ShortLinkResolutionError(
                "Bilibili share link target is unsupported",
                Platform.BILIBILI,
            )
        return classified
    except ShortLinkResolutionError:
        raise
    except Exception as exc:
        raise ShortLinkResolutionError(
            "Bilibili share link could not be resolved",
            Platform.BILIBILI,
        ) from exc
    finally:
        if response is not None:
            response.close()
