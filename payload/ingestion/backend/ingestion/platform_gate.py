from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import Platform


@dataclass(frozen=True)
class GateLease:
    allowed: bool
    platform: str
    probe: bool = False
    retry_at: datetime | None = None
    reason: str | None = None


class PlatformGateMixin:
    """Cross-process request pacing and circuit state stored in PostgreSQL."""

    def acquire_platform_request(
        self,
        platform: Platform | str,
        *,
        probe: bool = False,
        now: datetime | None = None,
        jitter_seconds: float | None = None,
    ) -> GateLease:
        platform_value = (
            platform.value if isinstance(platform, Platform) else str(platform)
        )
        if platform_value not in {"youtube", "bilibili", "xiaohongshu"}:
            raise ValueError("unsupported gated platform")
        now = now or datetime.now(timezone.utc)
        interval, jitter_max, _ = self._platform_policy(platform_value)
        jitter = (
            random.uniform(0, jitter_max)
            if jitter_seconds is None
            else max(0.0, min(float(jitter_seconds), float(jitter_max)))
        )
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO video_platform_request_gates (
                    platform, next_allowed_at, updated_at
                )
                VALUES (%s,%s,%s) ON CONFLICT (platform) DO NOTHING
                """,
                (platform_value, now, now),
            )
            gate = conn.execute(
                """
                SELECT * FROM video_platform_request_gates
                WHERE platform=%s FOR UPDATE
                """,
                (platform_value,),
            ).fetchone()
            state = gate["circuit_state"]
            if state == "OPEN":
                if gate["blocked_until"] is None:
                    if not probe:
                        return GateLease(
                            False, platform_value, retry_at=None,
                            reason=gate["last_error_code"] or "CIRCUIT_OPEN",
                        )
                    if gate["probe_in_flight"]:
                        return GateLease(
                            False, platform_value, reason="PROBE_IN_FLIGHT"
                        )
                    conn.execute(
                        """
                        UPDATE video_platform_request_gates
                        SET circuit_state='HALF_OPEN', probe_in_flight=true,
                            updated_at=%s
                        WHERE platform=%s
                        """,
                        (now, platform_value),
                    )
                    return GateLease(True, platform_value, probe=True)
                if now < gate["blocked_until"]:
                    return GateLease(
                        False, platform_value, retry_at=gate["blocked_until"],
                        reason=gate["last_error_code"] or "CIRCUIT_OPEN",
                    )
                if not probe:
                    return GateLease(
                        False, platform_value, retry_at=gate["blocked_until"],
                        reason="PROBE_REQUIRED",
                    )
                if gate["probe_in_flight"]:
                    return GateLease(False, platform_value, reason="PROBE_IN_FLIGHT")
                conn.execute(
                    """
                    UPDATE video_platform_request_gates
                    SET circuit_state='HALF_OPEN', probe_in_flight=true,
                        updated_at=%s
                    WHERE platform=%s
                    """,
                    (now, platform_value),
                )
                return GateLease(True, platform_value, probe=True)
            if state == "HALF_OPEN":
                return GateLease(False, platform_value, reason="PROBE_IN_FLIGHT")
            if probe:
                # A manual access probe still consumes the normal request slot.
                pass
            if now < gate["next_allowed_at"]:
                return GateLease(
                    False, platform_value, retry_at=gate["next_allowed_at"],
                    reason="REQUEST_INTERVAL",
                )
            next_allowed = now + timedelta(seconds=interval + jitter)
            conn.execute(
                """
                UPDATE video_platform_request_gates
                SET next_allowed_at=%s, updated_at=%s
                WHERE platform=%s
                """,
                (next_allowed, now, platform_value),
            )
            return GateLease(True, platform_value, probe=probe)

    def record_platform_success(
        self, platform: Platform | str, *, now: datetime | None = None
    ) -> dict[str, Any]:
        platform_value = (
            platform.value if isinstance(platform, Platform) else str(platform)
        )
        now = now or datetime.now(timezone.utc)
        interval, _, _ = self._platform_policy(platform_value)
        with self.connection() as conn:
            row = conn.execute(
                """
                UPDATE video_platform_request_gates
                SET circuit_state='CLOSED', blocked_until=NULL,
                    consecutive_failures=0, last_error_code=NULL,
                    probe_in_flight=false, opened_at=NULL,
                    next_allowed_at=%s,
                    last_success_at=%s, updated_at=%s
                WHERE platform=%s
                RETURNING *
                """,
                (
                    now + timedelta(seconds=interval),
                    now,
                    now,
                    platform_value,
                ),
            ).fetchone()
        if not row:
            raise KeyError(platform_value)
        return dict(row)

    def record_platform_failure(
        self,
        platform: Platform | str,
        error_code: str,
        *,
        immediate_open: bool = False,
        rate_limited: bool = False,
        retry_after_seconds: int | None = None,
        now: datetime | None = None,
        jitter_seconds: float = 0,
    ) -> dict[str, Any]:
        platform_value = (
            platform.value if isinstance(platform, Platform) else str(platform)
        )
        now = now or datetime.now(timezone.utc)
        _, jitter_max, base_backoff = self._platform_policy(platform_value)
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO video_platform_request_gates (platform)
                VALUES (%s) ON CONFLICT (platform) DO NOTHING
                """,
                (platform_value,),
            )
            gate = conn.execute(
                """
                SELECT * FROM video_platform_request_gates
                WHERE platform=%s FOR UPDATE
                """,
                (platform_value,),
            ).fetchone()
            recent = (
                gate["updated_at"] is not None
                and now - gate["updated_at"]
                <= timedelta(seconds=self.settings.platform_failure_window_seconds)
            )
            failures = (gate["consecutive_failures"] if recent else 0) + 1
            should_open = immediate_open or (
                rate_limited
                and failures >= self.settings.platform_failure_threshold
            )
            blocked_until = gate["blocked_until"]
            if should_open:
                indefinite = error_code in {
                    "AUTH_EXPIRED",
                    "BOT_CHECK",
                    "PLATFORM_BLOCKED",
                }
                if indefinite:
                    blocked_until = None
                else:
                    exponent = max(
                        0, failures - self.settings.platform_failure_threshold
                    )
                    delay = retry_after_seconds or min(
                        base_backoff * (2**exponent),
                        self.settings.platform_max_backoff_seconds,
                    )
                    delay += max(0, min(jitter_seconds, jitter_max))
                    blocked_until = now + timedelta(seconds=delay)
            row = conn.execute(
                """
                UPDATE video_platform_request_gates
                SET circuit_state=%s, blocked_until=%s,
                    consecutive_failures=%s, last_error_code=%s,
                    probe_in_flight=false,
                    opened_at=CASE WHEN %s='OPEN'
                        THEN coalesce(opened_at,%s) ELSE opened_at END,
                    updated_at=%s
                WHERE platform=%s
                RETURNING *
                """,
                (
                    "OPEN" if should_open else "CLOSED",
                    blocked_until if should_open else None,
                    failures,
                    error_code,
                    "OPEN" if should_open else "CLOSED",
                    now,
                    now,
                    platform_value,
                ),
            ).fetchone()
        return dict(row)

    def platform_gate(self, platform: Platform | str) -> dict[str, Any] | None:
        platform_value = (
            platform.value if isinstance(platform, Platform) else str(platform)
        )
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM video_platform_request_gates WHERE platform=%s",
                (platform_value,),
            ).fetchone()
        return dict(row) if row else None

    def _platform_policy(self, platform: str) -> tuple[int, int, int]:
        if platform == "youtube":
            return (
                self.settings.youtube_request_min_interval_seconds,
                self.settings.youtube_request_jitter_seconds,
                self.settings.youtube_rate_limit_backoff_seconds,
            )
        if platform == "bilibili":
            return (
                self.settings.bilibili_request_min_interval_seconds,
                self.settings.bilibili_request_jitter_seconds,
                self.settings.bilibili_rate_limit_backoff_seconds,
            )
        return (
            self.settings.xiaohongshu_request_min_interval_seconds,
            self.settings.xiaohongshu_request_jitter_seconds,
            self.settings.xiaohongshu_rate_limit_backoff_seconds,
        )
