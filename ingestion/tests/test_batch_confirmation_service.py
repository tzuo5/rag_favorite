from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.ingestion.batch_service import (
    BatchConfirmationDisabledError,
    BatchConfirmationService,
    BatchPlatformUnavailableError,
    BatchResourceError,
)
from backend.ingestion.config import Settings
from backend.ingestion.models import Destination


class FakeRepository:
    def __init__(self, settings: Settings, platform: str = "youtube"):
        self.settings = settings
        self.discovery = {
            "id": "discovery",
            "telegram_user_id": "user",
            "platform": platform,
        }
        self.confirmed = []
        self.gate = None

    def get_author_discovery(self, discovery_id, user_id=None):
        if (
            discovery_id != self.discovery["id"]
            or (
                user_id is not None
                and str(user_id) != self.discovery["telegram_user_id"]
            )
        ):
            return None
        return self.discovery

    def platform_gate(self, platform):
        return self.gate

    def confirm_discovery(
        self,
        discovery_id,
        user_id,
        destination,
        *,
        limit,
        selection_kind,
    ):
        self.confirmed.append(
            (discovery_id, user_id, destination, limit, selection_kind)
        )
        return {"id": "batch-id", "state": "QUEUED"}


def usage(free_gb: int):
    return lambda _: SimpleNamespace(free=free_gb * 2**30)


def test_confirmation_fails_closed_when_execution_is_disabled() -> None:
    repository = FakeRepository(Settings())
    with pytest.raises(BatchConfirmationDisabledError):
        BatchConfirmationService(repository, disk_usage=usage(20)).confirm(
            "discovery", "user", Destination.GENERAL
        )
    assert repository.confirmed == []


def test_confirmation_checks_disk_and_platform_before_transaction() -> None:
    settings = Settings(author_batch_youtube_execution_enabled=True)
    repository = FakeRepository(settings)
    with pytest.raises(BatchResourceError):
        BatchConfirmationService(repository, disk_usage=usage(4)).confirm(
            "discovery", "user", Destination.GENERAL
        )
    repository.gate = {
        "circuit_state": "OPEN",
        "last_error_code": "AUTH_EXPIRED",
    }
    with pytest.raises(BatchPlatformUnavailableError):
        BatchConfirmationService(repository, disk_usage=usage(20)).confirm(
            "discovery", "user", Destination.GENERAL
        )
    assert repository.confirmed == []


def test_confirmation_delegates_bounded_inputs() -> None:
    settings = Settings(author_batch_youtube_execution_enabled=True)
    repository = FakeRepository(settings)
    result = BatchConfirmationService(repository, disk_usage=usage(20)).confirm(
        "discovery", "user", Destination.COOKING, limit=7
    )
    assert result["id"] == "batch-id"
    assert repository.confirmed == [
        ("discovery", "user", Destination.COOKING, 7, "latest")
    ]


def test_confirmation_preserves_all_preview_selection() -> None:
    settings = Settings(author_batch_youtube_execution_enabled=True)
    repository = FakeRepository(settings)
    BatchConfirmationService(repository, disk_usage=usage(20)).confirm(
        "discovery",
        "user",
        Destination.GENERAL,
        limit=137,
        selection_kind="all_preview",
    )
    assert repository.confirmed[-1] == (
        "discovery",
        "user",
        Destination.GENERAL,
        137,
        "all_preview",
    )


def test_confirmation_uses_the_discovery_platform_flag_and_gate() -> None:
    settings = Settings(author_batch_bilibili_enabled=True)
    repository = FakeRepository(settings, platform="bilibili")
    result = BatchConfirmationService(repository, disk_usage=usage(20)).confirm(
        "discovery", "user", Destination.GENERAL, limit=10
    )
    assert result["state"] == "QUEUED"

    disabled = FakeRepository(Settings(), platform="bilibili")
    with pytest.raises(BatchConfirmationDisabledError):
        BatchConfirmationService(disabled, disk_usage=usage(20)).confirm(
            "discovery", "user", Destination.GENERAL
        )
