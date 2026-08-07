from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class JobState(StrEnum):
    RECEIVED = "RECEIVED"
    DOWNLOADING = "DOWNLOADING"
    EXTRACTING_SUBTITLES = "EXTRACTING_SUBTITLES"
    TRANSCRIBING = "TRANSCRIBING"
    BUILDING_MARKDOWN = "BUILDING_MARKDOWN"
    ENRICHING_METADATA = "ENRICHING_METADATA"
    AWAITING_DESTINATION = "AWAITING_DESTINATION"
    PERSISTING = "PERSISTING"
    PAUSED_USER = "PAUSED_USER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Platform(StrEnum):
    YOUTUBE = "youtube"
    BILIBILI = "bilibili"
    XIAOHONGSHU = "xiaohongshu"
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    TWITTER = "twitter"
    TELEGRAM = "telegram"
    OTHER = "other"


class Destination(StrEnum):
    # ``main`` is retained only so already-created jobs from deployments before
    # migration 0005 can finish. New user-facing flows must use one of the
    # explicit topic destinations below.
    MAIN = "main"
    THOUGHT_POLITICS = "thought-politics"
    TECH = "tech"
    FINANCE = "finance"
    CAREER = "career"
    SOCIAL_CONDUCT = "social-conduct"
    LITERATURE_CULTURE = "literature-culture"
    GENERAL = "general"
    COOKING = "cooking"


SELECTABLE_DESTINATIONS = (
    Destination.THOUGHT_POLITICS,
    Destination.TECH,
    Destination.FINANCE,
    Destination.CAREER,
    Destination.SOCIAL_CONDUCT,
    Destination.LITERATURE_CULTURE,
    Destination.GENERAL,
    Destination.COOKING,
)

DESTINATION_LABELS = {
    Destination.MAIN: "旧主知识库",
    Destination.THOUGHT_POLITICS: "思想、政治与社会议题",
    Destination.TECH: "技术",
    Destination.FINANCE: "金融与投资",
    Destination.CAREER: "职业发展",
    Destination.SOCIAL_CONDUCT: "中国人情世故",
    Destination.LITERATURE_CULTURE: "文学与文化",
    Destination.GENERAL: "综合资料",
    Destination.COOKING: "烹饪",
}

DESTINATION_CALLBACK_CODES = {
    Destination.THOUGHT_POLITICS: "tp",
    Destination.TECH: "te",
    Destination.FINANCE: "fi",
    Destination.CAREER: "ca",
    Destination.SOCIAL_CONDUCT: "sc",
    Destination.LITERATURE_CULTURE: "lc",
    Destination.GENERAL: "ge",
    Destination.COOKING: "co",
}


def destination_label(value: Destination | str | None) -> str:
    try:
        destination = value if isinstance(value, Destination) else Destination(value)
    except (TypeError, ValueError):
        return "未选择"
    return DESTINATION_LABELS[destination]


class DiscoveryState(StrEnum):
    QUEUED = "QUEUED"
    DISCOVERING = "DISCOVERING"
    READY = "READY"
    PAUSED_USER = "PAUSED_USER"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"


class WorkContentType(StrEnum):
    VIDEO = "VIDEO"
    SHORT = "SHORT"
    LIVE_REPLAY = "LIVE_REPLAY"
    UNKNOWN = "UNKNOWN"


class DiscoveryEligibility(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNSUPPORTED = "UNSUPPORTED"


class BatchState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED_USER = "PAUSED_USER"
    PAUSED_AUTH = "PAUSED_AUTH"
    PAUSED_RATE_LIMIT = "PAUSED_RATE_LIMIT"
    PAUSED_RESOURCE = "PAUSED_RESOURCE"
    CANCELLING = "CANCELLING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class BatchItemState(StrEnum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    SKIPPED_EXISTING = "SKIPPED_EXISTING"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class NotificationMode(StrEnum):
    INDIVIDUAL = "INDIVIDUAL"
    BATCH_SILENT = "BATCH_SILENT"


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class UrlKind(StrEnum):
    SINGLE_WORK = "SINGLE_WORK"
    AUTHOR_PAGE = "AUTHOR_PAGE"
    UNSUPPORTED_COLLECTION = "UNSUPPORTED_COLLECTION"
    UNKNOWN = "UNKNOWN"


class AccessProbeStatus(StrEnum):
    OK = "OK"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"


class PlatformErrorCode(StrEnum):
    AUTH_EXPIRED = "AUTH_EXPIRED"
    BOT_CHECK = "BOT_CHECK"
    RATE_LIMITED = "RATE_LIMITED"
    PLATFORM_BLOCKED = "PLATFORM_BLOCKED"
    PLATFORM_UNAVAILABLE = "PLATFORM_UNAVAILABLE"
    NETWORK_ERROR = "NETWORK_ERROR"
    EXTRACTOR_SCHEMA_CHANGED = "EXTRACTOR_SCHEMA_CHANGED"
    DISCOVERY_FAILED = "DISCOVERY_FAILED"


class BatchNotificationEventType(StrEnum):
    CREATED = "CREATED"
    PROGRESS = "PROGRESS"
    PAUSED = "PAUSED"
    RESUMED = "RESUMED"
    CANCELLING = "CANCELLING"
    TERMINAL = "TERMINAL"


class Enrichment(BaseModel):
    normalized_title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=4000)
    key_points: list[str] = Field(min_length=1, max_length=20)
    tags: list[str] = Field(min_length=8, max_length=20)

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str]) -> list[str]:
        banned = {"video", "knowledge", "interesting", "misc"}
        result: list[str] = []
        for item in value:
            tag = " ".join(item.strip().split())[:80]
            if tag and tag.lower() not in banned and tag not in result:
                result.append(tag)
        if len(result) < 8:
            raise ValueError("not enough meaningful tags")
        return result


class SourceMetadata(BaseModel):
    platform: Platform = Platform.OTHER
    canonical_url: str | None = None
    source_id: str | None = None
    original_title: str | None = None
    author: str | None = None
    uploader_id: str | None = None
    published_at: str | None = None
    duration_seconds: float | None = None
    description: str | None = None
    thumbnail_url: str | None = None
    language: str | None = None
    extractor: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
