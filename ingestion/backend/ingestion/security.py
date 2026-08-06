from __future__ import annotations

import ipaddress
import logging
import re
import socket
import unicodedata
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

URL_RE = re.compile(r"https?://[^\s<>\]\[\"']+", re.IGNORECASE)
SENSITIVE_VALUE_RE = re.compile(
    r"""(?ix)
    \b(
        xsec_token|access_token|refresh_token|id_token|api_key|apikey|
        authorization|cookie|password|passwd|secret|signature
    )
    (\s*["']?\s*[:=]\s*["']?)
    ([^&,\s;"']+)
    """
)
_YTDLP_LOGGER = logging.getLogger("backend.ingestion.yt_dlp")


def stable_source_url(value: str | None, platform: str | None = None) -> str | None:
    """Remove ephemeral sharing parameters while preserving content identity."""
    if not value:
        return None
    parsed = urlsplit(value)
    platform = (platform or "").lower()
    host = (parsed.hostname or "").lower()
    if platform in {"xiaohongshu", "bilibili", "tiktok", "instagram", "twitter"}:
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    if platform == "youtube" or host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        query = [(key, item) for key, item in parse_qsl(parsed.query) if key == "v"]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
    tracking = {"share_id", "xsec_token", "xhsshare", "apptime", "author_share", "share_from_user_hidden"}
    query = [(key, item) for key, item in parse_qsl(parsed.query) if not key.lower().startswith("utm_") and key.lower() not in tracking]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def redact_sensitive_text(value: object, max_length: int | None = None) -> str:
    """Remove URL credentials/query strings and common secret-bearing values."""
    text = str(value)

    def redact_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        parsed = urlsplit(raw)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port is not None:
            netloc = f"{netloc}:{port}"
        path = (
            "/[REDACTED]"
            if host.lower() == "xhscdn.com"
            or host.lower().endswith(".xhscdn.com")
            else parsed.path
        )
        return urlunsplit((parsed.scheme, netloc, path, "", ""))

    text = URL_RE.sub(redact_url, text)
    text = SENSITIVE_VALUE_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        text,
    )
    return text[:max_length] if max_length is not None else text


class SafeYtdlpLogger:
    """Prevent yt-dlp from writing raw URLs, cookies, or tokens to stderr."""

    def debug(self, message: object) -> None:
        # yt-dlp debug output can include request headers and cookie details.
        return None

    def warning(self, message: object) -> None:
        _YTDLP_LOGGER.warning(redact_sensitive_text(message, max_length=1000))

    def error(self, message: object) -> None:
        _YTDLP_LOGGER.error(redact_sensitive_text(message, max_length=1000))


def extract_urls(text: str) -> list[str]:
    return [match.rstrip(".,;:!?，。；：！？)") for match in URL_RE.findall(text or "")]


def validate_public_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("仅支持公开的 HTTP/HTTPS 视频链接")
    if parsed.username or parsed.password:
        raise ValueError("链接不得包含凭据")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"}:
        raise ValueError("不允许访问本机或内网地址")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443)}
    except socket.gaierror as exc:
        raise ValueError("链接域名无法解析") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("不允许访问本机或内网地址")
    return value


def safe_filename(value: str, fallback: str = "untitled", max_length: int = 100) -> str:
    value = unicodedata.normalize("NFKC", value or "").strip()
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    if not value or value in {".", ".."}:
        value = fallback
    return value[:max_length].rstrip(" .-") or fallback


def confined_media_path(value: str, allowed_roots: list[Path]) -> Path:
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("媒体文件不存在")
    for root in allowed_roots:
        try:
            path.relative_to(root.expanduser().resolve())
            return path
        except ValueError:
            continue
    raise ValueError("媒体文件不在允许的入站目录中")
