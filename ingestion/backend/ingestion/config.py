from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .product_config import product_config

ROOT = Path(__file__).resolve().parents[2]
PRODUCT_CONFIG = product_config()
PRODUCT_DATA = PRODUCT_CONFIG.paths.data_dir


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _path(name: str, default: str | Path) -> Path:
    return Path(os.getenv(name) or default).expanduser()


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    temp_root: Path = field(
        default_factory=lambda: _path("VIDEO_TEMP_ROOT", ROOT / "temp/jobs")
    )
    staging_root: Path = field(
        default_factory=lambda: _path("VIDEO_STAGING_ROOT", ROOT / "staging")
    )
    openclaw_media_root: Path = field(
        default_factory=lambda: _path(
            "OPENCLAW_MEDIA_ROOT", Path.home() / ".openclaw" / "media"
        )
    )
    test_fixture_root: Path = field(
        default_factory=lambda: _path("VIDEO_TEST_FIXTURE_ROOT", ROOT / "test-fixtures")
    )
    main_markdown_root: Path = field(
        default_factory=lambda: _path(
            "MAIN_KB_MARKDOWN_DIR",
            PRODUCT_CONFIG.collection("general").path / "video-transcripts",
        )
    )
    cooking_markdown_root: Path = field(
        default_factory=lambda: _path(
            "COOKING_KB_MARKDOWN_DIR",
            PRODUCT_CONFIG.collection("cooking").path / "video-transcripts",
        )
    )
    database_env: Path = field(
        default_factory=lambda: _path(
            "VIDEO_DB_ENV",
            PRODUCT_CONFIG.database.credentials_file
            or PRODUCT_CONFIG.paths.secrets_file,
        )
    )
    cooking_env: Path = field(
        default_factory=lambda: _path(
            "COOKING_INGEST_ENV",
            PRODUCT_CONFIG.database.credentials_file
            or PRODUCT_CONFIG.paths.secrets_file,
        )
    )
    max_file_size_mb: int = _int("MAX_FILE_SIZE_MB", 500)
    max_video_duration_seconds: int = _int("MAX_VIDEO_DURATION_SECONDS", 14400)
    max_concurrent_jobs: int = _int("MAX_CONCURRENT_JOBS", 1)
    job_timeout_seconds: int = _int("JOB_TIMEOUT_SECONDS", 21600)
    temp_file_ttl_hours: int = _int("TEMP_FILE_TTL_HOURS", 24)
    max_retries: int = _int("MAX_JOB_RETRIES", 2)
    job_retry_delay_seconds: int = _int("JOB_RETRY_DELAY_SECONDS", 60)
    author_batch_youtube_discovery_enabled: bool = _bool(
        "AUTHOR_BATCH_YOUTUBE_DISCOVERY_ENABLED"
    )
    author_batch_youtube_execution_enabled: bool = _bool(
        "AUTHOR_BATCH_YOUTUBE_EXECUTION_ENABLED"
    )
    author_batch_bilibili_enabled: bool = _bool("AUTHOR_BATCH_BILIBILI_ENABLED")
    author_batch_xiaohongshu_enabled: bool = _bool("AUTHOR_BATCH_XIAOHONGSHU_ENABLED")
    author_discovery_scan_limit: int = _int("AUTHOR_DISCOVERY_SCAN_LIMIT", 50)
    author_batch_max_items: int = _int("AUTHOR_BATCH_MAX_ITEMS", 50)
    author_discovery_preview_ttl_minutes: int = _int(
        "AUTHOR_DISCOVERY_PREVIEW_TTL_MINUTES", 30
    )
    max_batch_estimated_duration_seconds: int = _int(
        "MAX_BATCH_ESTIMATED_DURATION_SECONDS", 43200
    )
    enforce_batch_estimated_duration_budget: bool = _bool(
        "ENFORCE_BATCH_ESTIMATED_DURATION_BUDGET"
    )
    max_batch_unknown_duration_reservation_seconds: int = _int(
        "MAX_BATCH_UNKNOWN_DURATION_RESERVATION_SECONDS", 3600
    )
    batch_notification_item_threshold: int = _int(
        "BATCH_NOTIFICATION_ITEM_THRESHOLD", 5
    )
    batch_notification_interval_seconds: int = _int(
        "BATCH_NOTIFICATION_INTERVAL_SECONDS", 600
    )
    batch_child_priority_wait_seconds: int = _int(
        "BATCH_CHILD_PRIORITY_WAIT_SECONDS", 1800
    )
    min_disk_free_gb: int = _int("MIN_DISK_FREE_GB", 5)
    youtube_request_min_interval_seconds: int = _int(
        "YOUTUBE_REQUEST_MIN_INTERVAL_SECONDS", 5
    )
    youtube_request_jitter_seconds: int = _int("YOUTUBE_REQUEST_JITTER_SECONDS", 3)
    youtube_rate_limit_backoff_seconds: int = _int(
        "YOUTUBE_RATE_LIMIT_BACKOFF_SECONDS", 900
    )
    bilibili_request_min_interval_seconds: int = _int(
        "BILIBILI_REQUEST_MIN_INTERVAL_SECONDS", 10
    )
    bilibili_request_jitter_seconds: int = _int("BILIBILI_REQUEST_JITTER_SECONDS", 5)
    bilibili_rate_limit_backoff_seconds: int = _int(
        "BILIBILI_RATE_LIMIT_BACKOFF_SECONDS", 1800
    )
    xiaohongshu_request_min_interval_seconds: int = _int(
        "XIAOHONGSHU_REQUEST_MIN_INTERVAL_SECONDS", 15
    )
    xiaohongshu_request_jitter_seconds: int = _int(
        "XIAOHONGSHU_REQUEST_JITTER_SECONDS", 10
    )
    xiaohongshu_rate_limit_backoff_seconds: int = _int(
        "XIAOHONGSHU_RATE_LIMIT_BACKOFF_SECONDS", 3600
    )
    platform_failure_threshold: int = _int("PLATFORM_FAILURE_THRESHOLD", 3)
    platform_failure_window_seconds: int = _int("PLATFORM_FAILURE_WINDOW_SECONDS", 600)
    platform_max_backoff_seconds: int = _int("PLATFORM_MAX_BACKOFF_SECONDS", 21600)
    author_discovery_stale_seconds: int = _int("AUTHOR_DISCOVERY_STALE_SECONDS", 300)
    ytdlp_cookies_file: str | None = os.getenv("YTDLP_COOKIES_FILE") or None
    bilibili_cookies_file: str | None = (
        os.getenv("BILIBILI_COOKIES_FILE") or os.getenv("YTDLP_COOKIES_FILE") or None
    )
    xiaohongshu_cookies_file: str | None = (
        os.getenv("XIAOHONGSHU_COOKIES_FILE") or os.getenv("YTDLP_COOKIES_FILE") or None
    )
    xiaohongshu_session_enabled: bool = _bool("XIAOHONGSHU_SESSION_ENABLED")
    xiaohongshu_session_root: Path = field(
        default_factory=lambda: _path(
            "XIAOHONGSHU_SESSION_ROOT",
            PRODUCT_DATA / "sessions" / "xiaohongshu",
        )
    )
    xiaohongshu_session_probe_max_age_seconds: int = _int(
        "XIAOHONGSHU_SESSION_PROBE_MAX_AGE_SECONDS", 900
    )
    xiaohongshu_login_timeout_seconds: int = _int(
        "XIAOHONGSHU_LOGIN_TIMEOUT_SECONDS", 180
    )
    xiaohongshu_qr_ttl_seconds: int = _int("XIAOHONGSHU_QR_TTL_SECONDS", 300)
    xiaohongshu_auto_resume: bool = _bool("XIAOHONGSHU_AUTO_RESUME", True)
    xiaohongshu_login_chat_ids: frozenset[str] = frozenset(
        item.strip()
        for item in os.getenv(
            "XIAOHONGSHU_LOGIN_CHAT_IDS",
            os.getenv("TELEGRAM_ALLOWED_USER_IDS", ""),
        ).split(",")
        if item.strip()
    )
    bilibili_session_enabled: bool = _bool("BILIBILI_SESSION_ENABLED")
    bilibili_session_root: Path = field(
        default_factory=lambda: _path(
            "BILIBILI_SESSION_ROOT",
            PRODUCT_DATA / "sessions" / "bilibili",
        )
    )
    bilibili_login_timeout_seconds: int = _int("BILIBILI_LOGIN_TIMEOUT_SECONDS", 180)
    bilibili_qr_ttl_seconds: int = _int("BILIBILI_QR_TTL_SECONDS", 180)
    bilibili_auto_resume: bool = _bool("BILIBILI_AUTO_RESUME", True)
    bilibili_login_chat_ids: frozenset[str] = frozenset(
        item.strip()
        for item in os.getenv(
            "BILIBILI_LOGIN_CHAT_IDS",
            os.getenv("TELEGRAM_ALLOWED_USER_IDS", ""),
        ).split(",")
        if item.strip()
    )
    allowed_user_ids: frozenset[str] = frozenset(
        item.strip()
        for item in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
        if item.strip()
    )

    def __post_init__(self) -> None:
        if not 1 <= self.author_batch_max_items <= 50:
            raise ValueError("AUTHOR_BATCH_MAX_ITEMS must be between 1 and 50")
        if not self.author_batch_max_items <= self.author_discovery_scan_limit <= 50:
            raise ValueError(
                "AUTHOR_DISCOVERY_SCAN_LIMIT must be between the batch limit and 50"
            )
        positive = {
            "AUTHOR_DISCOVERY_PREVIEW_TTL_MINUTES": self.author_discovery_preview_ttl_minutes,
            "MAX_BATCH_ESTIMATED_DURATION_SECONDS": self.max_batch_estimated_duration_seconds,
            "MAX_BATCH_UNKNOWN_DURATION_RESERVATION_SECONDS": self.max_batch_unknown_duration_reservation_seconds,
            "BATCH_NOTIFICATION_ITEM_THRESHOLD": self.batch_notification_item_threshold,
            "BATCH_NOTIFICATION_INTERVAL_SECONDS": self.batch_notification_interval_seconds,
            "BATCH_CHILD_PRIORITY_WAIT_SECONDS": self.batch_child_priority_wait_seconds,
            "MIN_DISK_FREE_GB": self.min_disk_free_gb,
            "YOUTUBE_REQUEST_MIN_INTERVAL_SECONDS": self.youtube_request_min_interval_seconds,
            "YOUTUBE_RATE_LIMIT_BACKOFF_SECONDS": self.youtube_rate_limit_backoff_seconds,
            "BILIBILI_REQUEST_MIN_INTERVAL_SECONDS": self.bilibili_request_min_interval_seconds,
            "BILIBILI_RATE_LIMIT_BACKOFF_SECONDS": self.bilibili_rate_limit_backoff_seconds,
            "XIAOHONGSHU_REQUEST_MIN_INTERVAL_SECONDS": self.xiaohongshu_request_min_interval_seconds,
            "XIAOHONGSHU_RATE_LIMIT_BACKOFF_SECONDS": self.xiaohongshu_rate_limit_backoff_seconds,
            "PLATFORM_FAILURE_THRESHOLD": self.platform_failure_threshold,
            "PLATFORM_FAILURE_WINDOW_SECONDS": self.platform_failure_window_seconds,
            "PLATFORM_MAX_BACKOFF_SECONDS": self.platform_max_backoff_seconds,
            "AUTHOR_DISCOVERY_STALE_SECONDS": self.author_discovery_stale_seconds,
            "XIAOHONGSHU_SESSION_PROBE_MAX_AGE_SECONDS": self.xiaohongshu_session_probe_max_age_seconds,
            "XIAOHONGSHU_LOGIN_TIMEOUT_SECONDS": self.xiaohongshu_login_timeout_seconds,
            "XIAOHONGSHU_QR_TTL_SECONDS": self.xiaohongshu_qr_ttl_seconds,
            "BILIBILI_LOGIN_TIMEOUT_SECONDS": self.bilibili_login_timeout_seconds,
            "BILIBILI_QR_TTL_SECONDS": self.bilibili_qr_ttl_seconds,
            "JOB_RETRY_DELAY_SECONDS": self.job_retry_delay_seconds,
        }
        invalid = [name for name, value in positive.items() if value <= 0]
        if invalid:
            raise ValueError(f"{', '.join(invalid)} must be positive")
        jitters = {
            "YOUTUBE_REQUEST_JITTER_SECONDS": self.youtube_request_jitter_seconds,
            "BILIBILI_REQUEST_JITTER_SECONDS": self.bilibili_request_jitter_seconds,
            "XIAOHONGSHU_REQUEST_JITTER_SECONDS": self.xiaohongshu_request_jitter_seconds,
        }
        invalid_jitters = [name for name, value in jitters.items() if value < 0]
        if invalid_jitters:
            raise ValueError(f"{', '.join(invalid_jitters)} must not be negative")

    def ensure_directories(self) -> None:
        for path in (self.temp_root, self.staging_root):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    @property
    def xiaohongshu_profile_dir(self) -> Path:
        return self.xiaohongshu_session_root / "profile"

    @property
    def xiaohongshu_status_file(self) -> Path:
        return self.xiaohongshu_session_root / "state/session-status.json"

    @property
    def xiaohongshu_session_lock_file(self) -> Path:
        return self.xiaohongshu_session_root / "runtime/session.lock"

    @property
    def xiaohongshu_qr_file(self) -> Path:
        return self.xiaohongshu_session_root / "screenshots/login-qr.png"

    @property
    def bilibili_status_file(self) -> Path:
        return self.bilibili_session_root / "state/session-status.json"

    @property
    def bilibili_session_lock_file(self) -> Path:
        return self.bilibili_session_root / "runtime/session.lock"

    @property
    def bilibili_qr_file(self) -> Path:
        return self.bilibili_session_root / "screenshots/login-qr.png"
