from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..models import (
    SELECTABLE_DESTINATIONS,
    Platform,
    destination_label,
)
from .bilibili import BilibiliAuthorDiscoveryAdapter
from .xiaohongshu import (
    XiaohongshuAuthorDiscoveryAdapter,
    cookie_auth_status,
    cookie_file_diagnostics,
)
from .youtube import YouTubeAuthorDiscoveryAdapter

AUTHOR_BATCH_PLATFORMS = (
    Platform.YOUTUBE,
    Platform.BILIBILI,
    Platform.XIAOHONGSHU,
)
PLATFORM_LABELS = {
    Platform.YOUTUBE: "YouTube",
    Platform.BILIBILI: "Bilibili",
    Platform.XIAOHONGSHU: "小红书",
}
ALL_AUTHOR_WORKS = 0


def author_discovery_enabled(settings: Settings, platform: Platform | str) -> bool:
    platform = Platform(platform)
    return {
        Platform.YOUTUBE: settings.author_batch_youtube_discovery_enabled,
        Platform.BILIBILI: settings.author_batch_bilibili_enabled,
        Platform.XIAOHONGSHU: settings.author_batch_xiaohongshu_enabled,
    }.get(platform, False)


def author_execution_enabled(settings: Settings, platform: Platform | str) -> bool:
    platform = Platform(platform)
    return {
        Platform.YOUTUBE: settings.author_batch_youtube_execution_enabled,
        Platform.BILIBILI: settings.author_batch_bilibili_enabled,
        Platform.XIAOHONGSHU: settings.author_batch_xiaohongshu_enabled,
    }.get(platform, False)


def author_batch_limit(settings: Settings, platform: Platform | str) -> int:
    Platform(platform)
    return settings.author_batch_max_items


def author_scan_limit(settings: Settings, platform: Platform | str) -> int:
    Platform(platform)
    return ALL_AUTHOR_WORKS


@dataclass
class AuthorDiscoveryAdapterRegistry:
    settings: Settings
    factories: Mapping[Platform, Callable[[Settings], Any]] | None = None

    def __post_init__(self) -> None:
        if self.factories is None:
            self.factories = {
                Platform.YOUTUBE: YouTubeAuthorDiscoveryAdapter,
                Platform.BILIBILI: BilibiliAuthorDiscoveryAdapter,
                Platform.XIAOHONGSHU: XiaohongshuAuthorDiscoveryAdapter,
            }
        self._instances: dict[Platform, Any] = {}

    def for_platform(self, platform: Platform | str) -> Any:
        platform = Platform(platform)
        if platform not in AUTHOR_BATCH_PLATFORMS or platform not in self.factories:
            raise ValueError("author discovery is unsupported for this platform")
        if platform not in self._instances:
            self._instances[platform] = self.factories[platform](self.settings)
        return self._instances[platform]


def capability_payload(
    settings: Settings,
    database: dict[str, Any] | None = None,
) -> dict[str, Any]:
    author_batch = {}
    for platform in AUTHOR_BATCH_PLATFORMS:
        discovery = author_discovery_enabled(settings, platform)
        execution = author_execution_enabled(settings, platform)
        database_limit = (
            int(database.get("max_items") or 0)
            if database and database.get("status") == "ready"
            else None
        )
        database_ready = (
            None
            if database is None or database.get("status") == "unavailable"
            else platform.value in database.get("platforms", [])
        )
        database_all_items_ready = (
            None
            if database_ready is None
            else database_ready and database_limit == 0
        )
        author_batch[platform.value] = {
            "status": (
                "blocked_schema"
                if (
                    (discovery or execution)
                    and database_all_items_ready is False
                )
                else "enabled"
                if discovery and execution
                else "discovery_only"
                if discovery
                else "disabled"
            ),
            "discovery_enabled": discovery,
            "execution_enabled": execution,
            "max_items": (
                database_limit
                if database_all_items_ready is False
                else None
            ),
            "all_items_supported": database_all_items_ready is not False,
            "recent_selection_max_items": author_batch_limit(
                settings, platform
            ),
            "database_ready": database_ready,
            "database_all_items_ready": database_all_items_ready,
        }
        if platform == Platform.XIAOHONGSHU:
            from backend.xhs_session.state import read_status

            session = read_status(settings.xiaohongshu_status_file)
            author_batch[platform.value]["auth_status"] = (
                session["session_status"]
                if settings.xiaohongshu_session_enabled
                else cookie_auth_status(settings)
            )
            author_batch[platform.value]["cookie_file"] = (
                cookie_file_diagnostics(settings)
            )
            author_batch[platform.value]["session"] = {
                "enabled": settings.xiaohongshu_session_enabled,
                "session_status": session["session_status"],
                "last_probe_at": session["last_probe_at"],
                "last_success_at": session["last_success_at"],
                "manual_login_required":
                    session["manual_login_required"],
            }
        elif platform == Platform.BILIBILI:
            from backend.xhs_session.state import read_status

            session = read_status(settings.bilibili_status_file)
            author_batch[platform.value]["session"] = {
                "enabled": settings.bilibili_session_enabled,
                "session_status": session["session_status"],
                "last_probe_at": session["last_probe_at"],
                "last_success_at": session["last_success_at"],
                "manual_login_required":
                    session["manual_login_required"],
            }
    payload = {
        "schema_version": 1,
        "single_work": {
            "youtube": "supported",
            "bilibili": "supported",
            "xiaohongshu": "supported_with_auth_limits",
        },
        "author_batch": author_batch,
        "knowledge_destinations": [
            {
                "id": destination.value,
                "label": destination_label(destination),
            }
            for destination in SELECTABLE_DESTINATIONS
        ],
        "destination_selection": {
            "single_work": "before_processing",
            "author_batch": "once_per_batch_before_confirmation",
            "legacy_writable": False,
        },
        "commands": [
            "/video_status",
            "/video_pause",
            "/video_resume",
            "/video_cancel",
            "/video_login",
            "/video_batch_status",
            "/video_batch_pause",
            "/video_batch_resume",
            "/video_batch_cancel",
        ],
        "limits": {
            "global_max_items": None,
            "recent_selection_max_items": settings.author_batch_max_items,
            "discovery_scan_limit": settings.author_discovery_scan_limit,
            "all_items": "unlimited",
        },
        "notes": {
            "xiaohongshu": (
                "public profile fallback plus authenticated signed pagination; "
                "cookies stay local and authentication, captcha, or risk-control "
                "failures stop safely"
            )
        },
    }
    if database is not None:
        payload["database"] = database
    return payload
