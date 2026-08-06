from __future__ import annotations

from typing import Any

from .discovery.errors import classify_platform_error
from .discovery.models import DiscoveryAdapterError
from .models import Platform


class AuthorDiscoveryWorker:
    def __init__(self, repository: Any, adapter: Any, notifier: Any | None = None):
        self.repository = repository
        self.adapter = adapter
        self.notifier = notifier

    def _adapter_for(self, platform: Platform) -> Any:
        if hasattr(self.adapter, "for_platform"):
            return self.adapter.for_platform(platform)
        adapter_platform = getattr(self.adapter, "platform", platform)
        if Platform(adapter_platform) != platform:
            raise ValueError("discovery adapter does not match claimed platform")
        return self.adapter

    def _classify_error(
        self,
        platform: Platform,
        exc: Exception,
    ):
        if (
            platform == Platform.BILIBILI
            and self.repository.settings.bilibili_session_enabled
        ):
            from backend.bilibili_session.recovery import classify_session_error

            return classify_session_error(self.repository.settings, exc)
        return classify_platform_error(exc)

    def _trigger_auth_recovery(self, platform: Platform) -> None:
        if platform == Platform.XIAOHONGSHU:
            from backend.xhs_session.recovery import trigger_session_refresh
        elif platform == Platform.BILIBILI:
            from backend.bilibili_session.recovery import trigger_session_refresh
        else:
            return
        trigger_session_refresh(self.repository.settings)

    def _defer_platform_auth(
        self,
        discovery: dict[str, Any],
        error_code: str,
    ) -> dict[str, Any] | None:
        platform = Platform(discovery["platform"])
        if error_code != "AUTH_EXPIRED" or platform not in {
            Platform.XIAOHONGSHU,
            Platform.BILIBILI,
        }:
            return None
        queued = self.repository.requeue_author_discovery(
            discovery["id"], error_code
        )
        self._trigger_auth_recovery(platform)
        return queued

    async def run_once(self) -> dict[str, Any] | None:
        discovery = self.repository.claim_author_discovery()
        if not discovery:
            return None
        lease = self.repository.acquire_platform_request(
            discovery["platform"], probe=False
        )
        if not lease.allowed:
            queued = self.repository.requeue_author_discovery(
                discovery["id"], lease.reason or "PLATFORM_GATE"
            )
            if lease.reason == "AUTH_EXPIRED":
                self._trigger_auth_recovery(
                    Platform(discovery["platform"])
                )
            return {
                "id": str(discovery["id"]),
                "state": queued["state"],
                "retry_at": lease.retry_at.isoformat() if lease.retry_at else None,
            }
        platform = Platform(discovery["platform"])
        try:
            adapter = self._adapter_for(platform)
            result = await adapter.discover(
                discovery["canonical_author_url"],
                scan_limit=discovery["scan_limit"],
            )
        except DiscoveryAdapterError as exc:
            immediate_open = exc.code in {
                "AUTH_EXPIRED",
                "BOT_CHECK",
                "PLATFORM_BLOCKED",
            }
            self.repository.record_platform_failure(
                platform,
                exc.code,
                immediate_open=immediate_open,
                rate_limited=exc.code == "RATE_LIMITED",
            )
            deferred = self._defer_platform_auth(discovery, exc.code)
            if deferred is not None:
                return dict(deferred)
            failed = self.repository.fail_author_discovery(
                discovery["id"], exc.code, exc.safe_message
            )
            if self.notifier is not None:
                self.notifier.discovery_failed(failed)
            return dict(failed)
        except Exception as exc:  # noqa: BLE001 - normalize adapter failures
            classified = self._classify_error(platform, exc)
            self.repository.record_platform_failure(
                platform,
                classified.code.value,
                immediate_open=classified.immediate_open,
                rate_limited=classified.rate_limited,
            )
            deferred = self._defer_platform_auth(
                discovery, classified.code.value
            )
            if deferred is not None:
                return dict(deferred)
            failed = self.repository.fail_author_discovery(
                discovery["id"],
                classified.code.value,
                classified.safe_message,
            )
            if self.notifier is not None:
                self.notifier.discovery_failed(failed)
            return dict(failed)
        completed = self.repository.complete_author_discovery(
            discovery["id"], result
        )
        if completed["state"] != "READY":
            return completed
        self.repository.record_platform_success(platform)
        if self.notifier is not None:
            self.notifier.discovery_ready(completed)
        return completed
