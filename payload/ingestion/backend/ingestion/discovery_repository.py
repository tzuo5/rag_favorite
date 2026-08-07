from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg import errors
from psycopg.types.json import Jsonb

from .discovery.models import AuthorDiscoveryResult
from .discovery.registry import (
    ALL_AUTHOR_WORKS,
    AUTHOR_BATCH_PLATFORMS,
    author_scan_limit,
)
from .models import (
    SELECTABLE_DESTINATIONS,
    DiscoveryEligibility,
    Platform,
)


class DiscoveryRepositoryMixin:
    def create_author_discovery(
        self,
        *,
        user_id: str,
        chat_id: str,
        message_id: str,
        input_url: str,
        platform: Platform | str,
        canonical_author_url: str,
        scan_limit: int | None = None,
    ) -> dict[str, Any]:
        platform_value = (
            platform.value if isinstance(platform, Platform) else str(platform)
        )
        try:
            platform_enum = Platform(platform_value)
        except ValueError as exc:
            raise ValueError(
                "author discovery is not enabled for this platform"
            ) from exc
        if platform_enum not in AUTHOR_BATCH_PLATFORMS:
            raise ValueError("author discovery is not enabled for this platform")
        scan_limit = (
            author_scan_limit(self.settings, platform_enum)
            if scan_limit is None
            else scan_limit
        )
        if scan_limit != ALL_AUTHOR_WORKS and not (
            1 <= scan_limit <= self.settings.author_discovery_scan_limit
        ):
            raise ValueError("scan_limit is outside the configured range")
        discovery_id = uuid.uuid4()
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=self.settings.author_discovery_preview_ttl_minutes
        )
        try:
            with self.connection() as conn:
                row = conn.execute(
                    """
                    INSERT INTO video_author_discoveries (
                        id, telegram_user_id, telegram_chat_id,
                        telegram_message_id, input_url, platform,
                        canonical_author_url, state, scan_limit, expires_at
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,'QUEUED',%s,%s
                    )
                    RETURNING *
                    """,
                    (
                        discovery_id, str(user_id), str(chat_id), str(message_id),
                        canonical_author_url, platform_value, canonical_author_url,
                        scan_limit, expires_at,
                    ),
                ).fetchone()
            return dict(row)
        except errors.UniqueViolation:
            with self.connection() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM video_author_discoveries
                    WHERE telegram_user_id=%s AND platform=%s
                      AND canonical_author_url=%s
                      AND state IN (
                          'QUEUED','DISCOVERING','READY','PAUSED_USER'
                      )
                    ORDER BY created_at LIMIT 1
                    """,
                    (str(user_id), platform_value, canonical_author_url),
                ).fetchone()
            if not row:
                raise
            return dict(row)

    def claim_author_discovery(self) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                WITH candidate AS (
                    SELECT id FROM video_author_discoveries
                    WHERE state='QUEUED' AND expires_at > now()
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE video_author_discoveries discovery
                SET state='DISCOVERING', updated_at=now(),
                    error_code=NULL, error_message=NULL
                FROM candidate
                WHERE discovery.id=candidate.id
                RETURNING discovery.*
                """
            ).fetchone()
        return dict(row) if row else None

    def requeue_author_discovery(
        self, discovery_id: str | uuid.UUID, reason: str
    ) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute(
                """
                UPDATE video_author_discoveries
                SET state='QUEUED', error_code=%s, error_message=NULL,
                    updated_at=now()
                WHERE id=%s AND state='DISCOVERING'
                RETURNING *
                """,
                (reason, discovery_id),
            ).fetchone()
        if not row:
            current = self.get_author_discovery(discovery_id)
            if current and current["state"] in {
                "PAUSED_USER", "CANCELLED"
            }:
                return current
            raise KeyError(discovery_id)
        return dict(row)

    def resume_recovered_xiaohongshu_discoveries(self) -> int:
        """Wake non-expired XHS discoveries deferred for authentication."""
        with self.connection() as conn:
            rows = conn.execute(
                """
                UPDATE video_author_discoveries
                SET error_code=NULL, error_message=NULL, updated_at=now()
                WHERE platform='xiaohongshu'
                  AND state='QUEUED'
                  AND error_code='AUTH_EXPIRED'
                  AND expires_at > now()
                RETURNING id
                """
            ).fetchall()
        return len(rows)

    def resume_recovered_bilibili_discoveries(self) -> int:
        """Wake non-expired Bilibili discoveries deferred for authentication."""
        with self.connection() as conn:
            rows = conn.execute(
                """
                UPDATE video_author_discoveries
                SET error_code=NULL, error_message=NULL, updated_at=now()
                WHERE platform='bilibili'
                  AND state='QUEUED'
                  AND error_code='AUTH_EXPIRED'
                  AND expires_at > now()
                RETURNING id
                """
            ).fetchall()
        return len(rows)

    def complete_author_discovery(
        self,
        discovery_id: str | uuid.UUID,
        result: AuthorDiscoveryResult,
    ) -> dict[str, Any]:
        works = tuple(result.works)
        if len({work.source_id for work in works}) != len(works):
            raise ValueError("adapter returned duplicate source IDs")
        if len({work.position for work in works}) != len(works):
            raise ValueError("adapter returned duplicate positions")
        with self.connection() as conn:
            discovery = conn.execute(
                """
                SELECT * FROM video_author_discoveries
                WHERE id=%s FOR UPDATE
                """,
                (discovery_id,),
            ).fetchone()
            if not discovery:
                raise KeyError(discovery_id)
            if discovery["state"] == "READY":
                return dict(discovery)
            if discovery["state"] in {"PAUSED_USER", "CANCELLED"}:
                return dict(discovery)
            if discovery["state"] != "DISCOVERING":
                raise ValueError(
                    f"cannot complete discovery from {discovery['state']}"
                )
            if (
                discovery["scan_limit"] != ALL_AUTHOR_WORKS
                and len(works) > discovery["scan_limit"]
            ):
                raise ValueError("adapter returned more works than requested")
            if result.author.platform.value != discovery["platform"]:
                raise ValueError("adapter platform does not match discovery")
            if (
                result.author.canonical_url
                != discovery["canonical_author_url"]
            ):
                raise ValueError("adapter canonical author URL changed")
            conn.execute(
                "DELETE FROM video_author_discovery_items WHERE discovery_id=%s",
                (discovery_id,),
            )
            for work in works:
                conn.execute(
                    """
                    INSERT INTO video_author_discovery_items (
                        discovery_id, platform, source_id, canonical_url,
                        title, published_at, duration_seconds, content_type,
                        position, eligibility, raw_metadata
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        discovery_id, discovery["platform"], work.source_id,
                        work.canonical_url, work.title, work.published_at,
                        work.duration_seconds, work.content_type.value,
                        work.position, work.eligibility.value,
                        Jsonb(work.raw_metadata),
                    ),
                )
            existing = self._preview_existing_counts(conn, discovery_id)
            eligible_count = sum(
                work.eligibility == DiscoveryEligibility.ELIGIBLE for work in works
            )
            row = conn.execute(
                """
                UPDATE video_author_discoveries
                SET author_id=%s, author_name=%s, state='READY',
                    discovered_count=%s, eligible_count=%s,
                    existing_main_count=%s, existing_cooking_count=%s,
                    existing_destination_counts=%s,
                    error_code=NULL, error_message=NULL,
                    updated_at=now()
                WHERE id=%s
                RETURNING *
                """,
                (
                    result.author.author_id, result.author.display_name,
                    len(works), eligible_count, existing.get("main", 0),
                    existing["cooking"], Jsonb(existing), discovery_id,
                ),
            ).fetchone()
        return dict(row)

    def fail_author_discovery(
        self,
        discovery_id: str | uuid.UUID,
        error_code: str,
        safe_message: str,
    ) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute(
                """
                UPDATE video_author_discoveries
                SET state='FAILED', error_code=%s, error_message=%s,
                    finished_at=now(), updated_at=now()
                WHERE id=%s AND state IN ('QUEUED','DISCOVERING')
                RETURNING *
                """,
                (error_code, safe_message[:500], discovery_id),
            ).fetchone()
        if not row:
            current = self.get_author_discovery(discovery_id)
            if current and current["state"] in {
                "PAUSED_USER", "CANCELLED"
            }:
                return current
            raise KeyError(discovery_id)
        return dict(row)

    def get_author_discovery(
        self, discovery_id: str | uuid.UUID, user_id: str | None = None
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM video_author_discoveries WHERE id=%s"
        values: list[Any] = [discovery_id]
        if user_id is not None:
            query += " AND telegram_user_id=%s"
            values.append(str(user_id))
        with self.connection() as conn:
            row = conn.execute(query, values).fetchone()
        return dict(row) if row else None

    def latest_author_discovery(
        self, user_id: str
    ) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT discovery.*
                FROM video_author_discoveries discovery
                WHERE discovery.telegram_user_id=%s
                  AND NOT EXISTS (
                      SELECT 1 FROM video_ingestion_batches batch
                      WHERE batch.discovery_id=discovery.id
                  )
                ORDER BY
                    (discovery.state=ANY(%s)) DESC,
                    discovery.created_at DESC
                LIMIT 1
                """,
                (
                    str(user_id),
                    ["QUEUED", "DISCOVERING", "READY", "PAUSED_USER"],
                ),
            ).fetchone()
        return dict(row) if row else None

    def pause_author_discovery(
        self, discovery_id: str | uuid.UUID, user_id: str
    ) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute(
                """
                UPDATE video_author_discoveries
                SET state='PAUSED_USER', error_code='USER_PAUSED',
                    error_message='Paused by user', updated_at=now()
                WHERE id=%s AND telegram_user_id=%s
                  AND state IN (
                      'QUEUED','DISCOVERING','READY','PAUSED_USER'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM video_ingestion_batches batch
                      WHERE batch.discovery_id=video_author_discoveries.id
                  )
                RETURNING *
                """,
                (discovery_id, str(user_id)),
            ).fetchone()
        if not row:
            raise ValueError("author discovery cannot be paused")
        return dict(row)

    def resume_author_discovery(
        self, discovery_id: str | uuid.UUID, user_id: str
    ) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute(
                """
                UPDATE video_author_discoveries
                SET state=CASE WHEN author_id IS NULL
                               THEN 'QUEUED' ELSE 'READY' END,
                    error_code=NULL, error_message=NULL,
                    finished_at=NULL, updated_at=now()
                WHERE id=%s AND telegram_user_id=%s
                  AND state='PAUSED_USER'
                  AND expires_at > now()
                  AND NOT EXISTS (
                      SELECT 1 FROM video_ingestion_batches batch
                      WHERE batch.discovery_id=video_author_discoveries.id
                  )
                RETURNING *
                """,
                (discovery_id, str(user_id)),
            ).fetchone()
        if not row:
            raise ValueError("author discovery cannot be resumed")
        return dict(row)

    def cancel_author_discovery(
        self, discovery_id: str | uuid.UUID, user_id: str
    ) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute(
                """
                UPDATE video_author_discoveries
                SET state='CANCELLED', error_code='USER_CANCELLED',
                    error_message='Cancelled by user',
                    finished_at=now(), updated_at=now()
                WHERE id=%s AND telegram_user_id=%s
                  AND state IN (
                      'QUEUED','DISCOVERING','READY','PAUSED_USER'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM video_ingestion_batches batch
                      WHERE batch.discovery_id=video_author_discoveries.id
                  )
                RETURNING *
                """,
                (discovery_id, str(user_id)),
            ).fetchone()
        if not row:
            raise ValueError("author discovery cannot be cancelled")
        return dict(row)

    def expire_author_discoveries(self) -> int:
        with self.connection() as conn:
            rows = conn.execute(
                """
                UPDATE video_author_discoveries
                SET state='EXPIRED', finished_at=now(), updated_at=now()
                WHERE state IN ('QUEUED','READY','PAUSED_USER')
                  AND expires_at <= now()
                RETURNING id
                """
            ).fetchall()
        return len(rows)

    def recover_stale_author_discoveries(
        self, stale_after_seconds: int | None = None
    ) -> int:
        stale = (
            self.settings.author_discovery_stale_seconds
            if stale_after_seconds is None
            else max(0, stale_after_seconds)
        )
        with self.connection() as conn:
            rows = conn.execute(
                """
                UPDATE video_author_discoveries
                SET state='QUEUED', error_code='WORKER_RESTART',
                    error_message=NULL, updated_at=now()
                WHERE state='DISCOVERING'
                  AND updated_at <= now() - make_interval(secs => %s)
                  AND expires_at > now()
                RETURNING id
                """,
                (stale,),
            ).fetchall()
        return len(rows)

    @staticmethod
    def _preview_existing_counts(
        conn: Any, discovery_id: str | uuid.UUID
    ) -> dict[str, int]:
        rows = conn.execute(
            """
            SELECT document.selected_knowledge_base, count(DISTINCT item.source_id) AS count
            FROM video_author_discovery_items item
            JOIN video_knowledge_documents document
              ON document.source_platform=item.platform
             AND (
                  document.source_id=item.source_id
                  OR (
                      document.source_id IS NULL
                      AND document.source_url=item.canonical_url
                  )
             )
             AND document.ingestion_status='completed'
            WHERE item.discovery_id=%s
            GROUP BY document.selected_knowledge_base
            """,
            (discovery_id,),
        ).fetchall()
        counts = {
            destination.value: 0
            for destination in SELECTABLE_DESTINATIONS
        }
        for row in rows:
            if row["selected_knowledge_base"] in counts:
                counts[row["selected_knowledge_base"]] = row["count"]
        return counts
