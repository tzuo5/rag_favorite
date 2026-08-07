from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..models import (
    AccessProbeStatus,
    DiscoveryEligibility,
    Platform,
    WorkContentType,
)


@dataclass(frozen=True)
class AuthorSnapshot:
    platform: Platform
    author_id: str
    canonical_url: str
    display_name: str | None


@dataclass(frozen=True)
class DiscoveredWork:
    source_id: str
    canonical_url: str
    title: str | None
    published_at: datetime | None
    duration_seconds: float | None
    content_type: WorkContentType
    position: int
    eligibility: DiscoveryEligibility
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuthorDiscoveryResult:
    author: AuthorSnapshot
    works: tuple[DiscoveredWork, ...]
    extractor_name: str
    truncated: bool


@dataclass(frozen=True)
class AccessProbeResult:
    status: AccessProbeStatus
    retry_after_seconds: int | None = None
    error_code: str | None = None


class DiscoveryAdapterError(RuntimeError):
    def __init__(self, code: str, safe_message: str):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
