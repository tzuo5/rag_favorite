from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from backend.ingestion.models import (
    BatchItemState,
    BatchNotificationEventType,
    BatchState,
    CircuitState,
    DiscoveryEligibility,
    DiscoveryState,
    NotificationMode,
    SELECTABLE_DESTINATIONS,
    WorkContentType,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations/0003_author_batch_ingestion.sql"
ROLLBACK = ROOT / "migrations/rollback/0003_author_batch_ingestion.sql"
MULTIPLATFORM_MIGRATION = ROOT / "migrations/0004_multiplatform_author_batch.sql"
MULTIPLATFORM_ROLLBACK = (
    ROOT / "migrations/rollback/0004_multiplatform_author_batch.sql"
)
DESTINATION_MIGRATION = ROOT / "migrations/0005_explicit_knowledge_destinations.sql"
DESTINATION_ROLLBACK = (
    ROOT / "migrations/rollback/0005_explicit_knowledge_destinations.sql"
)
UNLIMITED_MIGRATION = ROOT / "migrations/0006_unlimited_author_batches.sql"
UNLIMITED_ROLLBACK = (
    ROOT / "migrations/rollback/0006_unlimited_author_batches.sql"
)
WORK_CONTROLS_MIGRATION = ROOT / "migrations/0007_work_controls.sql"
WORK_CONTROLS_ROLLBACK = (
    ROOT / "migrations/rollback/0007_work_controls.sql"
)


def values(enum_type: type) -> set[str]:
    return {item.value for item in enum_type}


def test_author_batch_application_enums_match_the_design() -> None:
    assert values(DiscoveryState) == {
        "QUEUED", "DISCOVERING", "READY", "PAUSED_USER", "CANCELLED",
        "FAILED", "EXPIRED", "CONSUMED",
    }
    assert values(WorkContentType) == {"VIDEO", "SHORT", "LIVE_REPLAY", "UNKNOWN"}
    assert values(DiscoveryEligibility) == {
        "ELIGIBLE", "UNAVAILABLE", "UNSUPPORTED",
    }
    assert values(BatchState) == {
        "QUEUED", "RUNNING", "PAUSED_USER", "PAUSED_AUTH",
        "PAUSED_RATE_LIMIT", "PAUSED_RESOURCE", "CANCELLING",
        "COMPLETED", "COMPLETED_WITH_ERRORS", "CANCELLED", "FAILED",
    }
    assert values(BatchItemState) == {
        "PENDING", "QUEUED", "RUNNING", "COMPLETED",
        "SKIPPED_EXISTING", "FAILED", "CANCELLED",
    }
    assert values(NotificationMode) == {"INDIVIDUAL", "BATCH_SILENT"}
    assert values(CircuitState) == {"CLOSED", "OPEN", "HALF_OPEN"}
    assert values(BatchNotificationEventType) == {
        "CREATED", "PROGRESS", "PAUSED", "RESUMED", "CANCELLING", "TERMINAL",
    }
    assert {item.value for item in SELECTABLE_DESTINATIONS} == {
        "thought-politics", "tech", "finance", "career", "social-conduct",
        "literature-culture", "general", "cooking",
    }


def test_author_batch_flags_default_to_disabled(
    monkeypatch,
) -> None:
    names = (
        "AUTHOR_BATCH_YOUTUBE_DISCOVERY_ENABLED",
        "AUTHOR_BATCH_YOUTUBE_EXECUTION_ENABLED",
        "AUTHOR_BATCH_BILIBILI_ENABLED",
        "AUTHOR_BATCH_XIAOHONGSHU_ENABLED",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    import backend.ingestion.config as config

    config = importlib.reload(config)
    settings = config.Settings()
    assert settings.author_batch_youtube_discovery_enabled is False
    assert settings.author_batch_youtube_execution_enabled is False
    assert settings.author_batch_bilibili_enabled is False
    assert settings.author_batch_xiaohongshu_enabled is False
    assert settings.author_discovery_scan_limit == 50
    assert settings.author_batch_max_items == 50
    assert settings.author_discovery_preview_ttl_minutes == 30
    assert settings.max_batch_estimated_duration_seconds == 43200
    assert not settings.enforce_batch_estimated_duration_budget
    assert settings.max_batch_unknown_duration_reservation_seconds == 3600


def test_0003_declares_all_six_tables_and_safe_job_defaults() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for table in (
        "video_author_discoveries",
        "video_author_discovery_items",
        "video_ingestion_batches",
        "video_ingestion_batch_items",
        "video_platform_request_gates",
        "video_ingestion_batch_notifications",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "notification_mode text NOT NULL DEFAULT 'INDIVIDUAL'" in sql
    assert "destination_locked boolean NOT NULL DEFAULT false" in sql
    assert "video_ingestion_batches_active_user_key" in sql
    assert "REFERENCES video_knowledge_documents(record_id)" in sql
    assert MIGRATION.name in "0003_author_batch_ingestion.sql"
    assert ROLLBACK.exists()
    assert "bilibili" in sql
    assert "xiaohongshu" in sql
    assert "total_count BETWEEN 1 AND 50" in sql


def test_0004_upgrades_existing_constraints_and_has_guarded_rollback() -> None:
    sql = MULTIPLATFORM_MIGRATION.read_text(encoding="utf-8")
    rollback = MULTIPLATFORM_ROLLBACK.read_text(encoding="utf-8")
    for table in (
        "video_author_discoveries",
        "video_author_discovery_items",
        "video_ingestion_batches",
        "video_ingestion_batch_items",
    ):
        assert f"ALTER TABLE {table}" in sql
    assert "bilibili" in sql and "xiaohongshu" in sql
    assert "total_count BETWEEN 1 AND 50" in sql
    assert "rollback refused" in rollback
    assert "platform <> 'youtube'" in rollback


def test_0005_expands_destinations_and_preserves_guarded_rollback() -> None:
    sql = DESTINATION_MIGRATION.read_text(encoding="utf-8")
    rollback = DESTINATION_ROLLBACK.read_text(encoding="utf-8")
    for destination in (
        "thought-politics", "tech", "finance", "career", "social-conduct",
        "literature-culture", "general", "cooking",
    ):
        assert f"'{destination}'" in sql
    assert "existing_destination_counts jsonb" in sql
    assert "WHERE selected_knowledge_base = 'main'" in sql
    assert "rollback refused" in rollback


def test_0006_allows_complete_author_discoveries_and_batches() -> None:
    sql = UNLIMITED_MIGRATION.read_text(encoding="utf-8")
    rollback = UNLIMITED_ROLLBACK.read_text(encoding="utf-8")
    assert "CHECK (scan_limit >= 0)" in sql
    assert "CHECK (total_count >= 1)" in sql
    assert "BETWEEN 1 AND 50" not in sql
    assert "rollback refused" in rollback


def test_0007_adds_single_and_discovery_work_controls() -> None:
    sql = WORK_CONTROLS_MIGRATION.read_text(encoding="utf-8")
    rollback = WORK_CONTROLS_ROLLBACK.read_text(encoding="utf-8")
    assert "'PERSISTING','PAUSED_USER','COMPLETED'" in sql
    assert "'READY','PAUSED_USER','CANCELLED'" in sql
    assert "video_author_discoveries_active_key" in sql
    assert "rollback refused" in rollback


def test_author_batch_hard_limits_are_validated() -> None:
    from backend.ingestion.config import Settings

    with pytest.raises(ValueError, match="AUTHOR_BATCH_MAX_ITEMS"):
        Settings(author_batch_max_items=51)
    with pytest.raises(ValueError, match="AUTHOR_DISCOVERY_SCAN_LIMIT"):
        Settings(author_batch_max_items=20, author_discovery_scan_limit=19)


def test_capability_registry_separates_single_work_and_author_batch() -> None:
    from backend.ingestion.config import Settings
    from backend.ingestion.discovery.registry import capability_payload

    payload = capability_payload(Settings(
        author_batch_youtube_discovery_enabled=True,
        author_batch_youtube_execution_enabled=True,
        author_batch_bilibili_enabled=True,
        author_batch_xiaohongshu_enabled=False,
    ))
    assert payload["single_work"]["xiaohongshu"] == (
        "supported_with_auth_limits"
    )
    assert payload["author_batch"]["youtube"]["status"] == "enabled"
    assert payload["author_batch"]["youtube"]["max_items"] is None
    assert payload["author_batch"]["bilibili"]["status"] == "enabled"
    assert payload["author_batch"]["bilibili"]["max_items"] is None
    assert payload["author_batch"]["bilibili"]["all_items_supported"] is True
    assert payload["author_batch"]["xiaohongshu"]["status"] == "disabled"
    assert payload["author_batch"]["xiaohongshu"]["max_items"] is None
    assert payload["author_batch"]["youtube"][
        "recent_selection_max_items"
    ] == 50
    assert payload["limits"]["all_items"] == "unlimited"
    assert payload["author_batch"]["xiaohongshu"]["auth_status"] in {
        "missing", "incomplete", "ready",
    }
    assert [row["id"] for row in payload["knowledge_destinations"]] == [
        "thought-politics", "tech", "finance", "career", "social-conduct",
        "literature-culture", "general", "cooking",
    ]
    assert payload["destination_selection"]["single_work"] == (
        "before_processing"
    )


def test_capability_payload_reports_effective_legacy_schema_limit() -> None:
    from backend.ingestion.config import Settings
    from backend.ingestion.discovery.registry import capability_payload

    payload = capability_payload(
        Settings(
            author_batch_youtube_discovery_enabled=True,
            author_batch_youtube_execution_enabled=True,
            author_batch_bilibili_enabled=True,
        ),
        database={
            "status": "ready",
            "platforms": ["youtube"],
            "max_items": 10,
        },
    )
    assert payload["author_batch"]["youtube"]["status"] == "blocked_schema"
    assert payload["author_batch"]["youtube"]["max_items"] == 10
    assert payload["author_batch"]["youtube"]["all_items_supported"] is False
    assert payload["author_batch"]["bilibili"]["status"] == "blocked_schema"
    assert payload["author_batch"]["bilibili"]["database_ready"] is False
