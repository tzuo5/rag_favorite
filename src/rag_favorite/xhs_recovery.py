"""Recover source credentials by exact note identity; never infer by title."""

from urllib.parse import parse_qs, urlsplit

from .config import ConfigError
from .video_sources import normalize_media_url


def identity(url):
    if not isinstance(url, str):
        return None
    try:
        key, _ = normalize_media_url(url)
        return key if key.startswith("xhs:") else None
    except (ConfigError, ValueError):
        return None


def signed(url):
    return bool(identity(url) and parse_qs(urlsplit(url).query).get("xsec_token"))


def collect_candidates(value, candidates):
    """Newest records should be visited first. Keep independent URL/author evidence."""
    if isinstance(value, dict):
        url = value.get("source_url") or value.get("url")
        key = identity(url)
        if key:
            candidate = candidates.setdefault(key, {})
            if signed(url):
                candidate.setdefault("source_url", url)
            if value.get("source_author_id") and not candidate.get("source_author_id"):
                candidate["source_author_id"] = value["source_author_id"]
                candidate["source_page_cursor"] = value.get("source_page_cursor", "")
        for child in value.values():
            collect_candidates(child, candidates)
    elif isinstance(value, list):
        for child in value:
            collect_candidates(child, candidates)
    elif signed(value):
        candidates.setdefault(identity(value), {}).setdefault("source_url", value)


def recover_payload(payload, candidates):
    result = dict(payload)
    candidate = candidates.get(identity(payload.get("source_url")), {})
    restored = False
    if not signed(result.get("source_url")) and candidate.get("source_url"):
        result["source_url"] = candidate["source_url"]
        restored = True
    if not result.get("source_author_id") and candidate.get("source_author_id"):
        result.update(
            {key: candidate[key] for key in ("source_author_id", "source_page_cursor")}
        )
        restored = True
    usable = signed(result.get("source_url")) or bool(result.get("source_author_id"))
    return (
        result,
        ("awaiting_source_validation" if usable else "needs_source_url"),
        restored,
    )
