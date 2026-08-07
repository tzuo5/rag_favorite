from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .batch_repository import BatchNotFoundError, BatchRepositoryError
from .discovery.registry import author_execution_enabled
from .models import SELECTABLE_DESTINATIONS, Destination, Platform


class BatchConfirmationDisabledError(BatchRepositoryError):
    pass


class BatchResourceError(BatchRepositoryError):
    pass


class BatchPlatformUnavailableError(BatchRepositoryError):
    pass


@dataclass
class BatchConfirmationService:
    repository: Any
    disk_usage: Callable[[object], Any] = shutil.disk_usage
    session_status_reader: Callable[[object], dict[str, Any]] | None = None
    session_probe: Callable[[object], bool] | None = None

    def confirm(
        self,
        discovery_id: str,
        user_id: str,
        destination: Destination,
        *,
        limit: int = 10,
        selection_kind: str = "latest",
    ) -> dict[str, Any]:
        if destination not in SELECTABLE_DESTINATIONS:
            raise ValueError("destination is not user-selectable")
        settings = self.repository.settings
        discovery = self.repository.get_author_discovery(discovery_id, user_id)
        if not discovery:
            raise BatchNotFoundError(f"discovery not found: {discovery_id}")
        platform = Platform(discovery["platform"])
        if not author_execution_enabled(settings, platform):
            raise BatchConfirmationDisabledError("batch execution is disabled")
        if (
            platform == Platform.XIAOHONGSHU
            and settings.xiaohongshu_session_enabled
        ):
            from backend.xhs_session.recovery import (
                probe_session_now,
                trigger_session_refresh,
            )
            from backend.xhs_session.state import (
                read_status,
                status_is_fresh_and_valid,
            )

            status = (
                self.session_status_reader(settings)
                if self.session_status_reader is not None
                else read_status(settings.xiaohongshu_status_file)
            )
            if not status_is_fresh_and_valid(
                status,
                max_age_seconds=(
                    settings.xiaohongshu_session_probe_max_age_seconds
                ),
            ):
                probe_succeeded = (
                    self.session_probe(settings)
                    if self.session_probe is not None
                    else probe_session_now(settings)
                )
                status = (
                    self.session_status_reader(settings)
                    if self.session_status_reader is not None
                    else read_status(settings.xiaohongshu_status_file)
                )
                if (
                    not probe_succeeded
                    or not status_is_fresh_and_valid(
                        status,
                        max_age_seconds=(
                            settings
                            .xiaohongshu_session_probe_max_age_seconds
                        ),
                    )
                ):
                    trigger_session_refresh(settings)
                    raise BatchPlatformUnavailableError(
                        "a fresh Xiaohongshu session probe is required"
                    )
        free_gb = self.disk_usage(settings.temp_root).free / 2**30
        if free_gb < settings.min_disk_free_gb:
            raise BatchResourceError(
                f"insufficient disk: {free_gb:.2f} GiB available"
            )
        gate = self.repository.platform_gate(platform)
        if gate and gate["circuit_state"] != "CLOSED":
            raise BatchPlatformUnavailableError(
                gate.get("last_error_code") or "platform circuit is open"
            )
        return self.repository.confirm_discovery(
            discovery_id,
            user_id,
            destination,
            limit=limit,
            selection_kind=selection_kind,
        )
