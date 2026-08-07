from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from .batch_presenter import batch_text, video_stage_label
from .discovery.registry import author_batch_limit
from .models import BatchState, Destination

ACTIVE_BATCH_STATES = (
    "QUEUED",
    "RUNNING",
    "PAUSED_USER",
    "PAUSED_AUTH",
    "PAUSED_RATE_LIMIT",
    "PAUSED_RESOURCE",
    "CANCELLING",
)
PAUSED_BATCH_STATES = (
    "PAUSED_USER",
    "PAUSED_AUTH",
    "PAUSED_RATE_LIMIT",
    "PAUSED_RESOURCE",
)
TERMINAL_ITEM_STATES = (
    "COMPLETED",
    "SKIPPED_EXISTING",
    "FAILED",
    "CANCELLED",
)


class BatchRepositoryError(RuntimeError):
    """Base class for deterministic batch transaction failures."""


class BatchNotFoundError(BatchRepositoryError):
    pass


class BatchForbiddenError(BatchRepositoryError):
    pass


class BatchStateError(BatchRepositoryError):
    pass


class ActiveBatchExistsError(BatchRepositoryError):
    def __init__(self, batch_id: uuid.UUID):
        super().__init__(f"active batch already exists: {batch_id}")
        self.batch_id = batch_id


class BatchBudgetExceededError(BatchRepositoryError):
    def __init__(self, estimated_seconds: int, maximum_seconds: int):
        super().__init__(
            f"batch duration budget exceeded: {estimated_seconds} > {maximum_seconds}"
        )
        self.estimated_seconds = estimated_seconds
        self.maximum_seconds = maximum_seconds


class BatchRepositoryMixin:
    """Atomic job/item/batch operations used by the batch orchestration layers."""

    def confirm_discovery(
        self,
        discovery_id: str | uuid.UUID,
        user_id: str,
        destination: Destination | str,
        *,
        limit: int = 10,
        selection_kind: str = "latest",
    ) -> dict[str, Any]:
        destination_value = (
            destination.value if isinstance(destination, Destination) else str(destination)
        )
        if destination_value not in {item.value for item in Destination}:
            raise ValueError("unsupported destination")
        if limit < 1 or (
            selection_kind != "all_preview"
            and limit > self.settings.author_batch_max_items
        ):
            raise ValueError("batch limit is outside the configured range")
        if selection_kind not in {"latest", "all_preview"}:
            raise ValueError("unsupported selection kind")

        with self.connection() as conn:
            discovery = conn.execute(
                """
                SELECT * FROM video_author_discoveries
                WHERE id=%s
                FOR UPDATE
                """,
                (discovery_id,),
            ).fetchone()
            if not discovery:
                raise BatchNotFoundError(f"discovery not found: {discovery_id}")
            if str(discovery["telegram_user_id"]) != str(user_id):
                raise BatchForbiddenError("discovery belongs to another user")

            existing = conn.execute(
                "SELECT * FROM video_ingestion_batches WHERE discovery_id=%s",
                (discovery_id,),
            ).fetchone()
            if existing:
                return dict(existing)
            if discovery["state"] != "READY":
                raise BatchStateError(
                    f"discovery cannot be confirmed from {discovery['state']}"
                )
            if discovery["expires_at"] <= conn.execute(
                "SELECT now() AS value"
            ).fetchone()["value"]:
                raise BatchStateError("discovery has expired")
            if (
                selection_kind != "all_preview"
                and limit > author_batch_limit(
                    self.settings, discovery["platform"]
                )
            ):
                raise ValueError("batch limit exceeds the platform safety limit")
            if (
                selection_kind == "all_preview"
                and limit != discovery["eligible_count"]
            ):
                raise ValueError(
                    "all-preview selection must include every eligible preview item"
                )

            # Serializes confirmations for different discoveries belonging to one user.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (str(user_id),),
            )
            active = conn.execute(
                """
                SELECT id FROM video_ingestion_batches
                WHERE telegram_user_id=%s AND state=ANY(%s)
                ORDER BY created_at LIMIT 1
                """,
                (str(user_id), list(ACTIVE_BATCH_STATES)),
            ).fetchone()
            if active:
                raise ActiveBatchExistsError(active["id"])

            selected = conn.execute(
                """
                SELECT * FROM video_author_discovery_items
                WHERE discovery_id=%s AND eligibility='ELIGIBLE'
                ORDER BY position
                LIMIT %s
                """,
                (discovery_id, limit),
            ).fetchall()
            if not selected:
                raise BatchStateError("discovery has no eligible works")
            estimated_seconds = sum(
                int(work["duration_seconds"])
                if work["duration_seconds"] is not None
                else self.settings.max_batch_unknown_duration_reservation_seconds
                for work in selected
            )
            if (
                self.settings.enforce_batch_estimated_duration_budget
                and estimated_seconds
                > self.settings.max_batch_estimated_duration_seconds
            ):
                raise BatchBudgetExceededError(
                    estimated_seconds,
                    self.settings.max_batch_estimated_duration_seconds,
                )

            batch_id = uuid.uuid5(
                uuid.NAMESPACE_URL, f"video-author-batch:{discovery_id}"
            )
            batch = conn.execute(
                """
                INSERT INTO video_ingestion_batches (
                    id, discovery_id, telegram_user_id, telegram_chat_id,
                    telegram_message_id, platform, author_id, author_url,
                    author_name, selection_policy, selected_destination,
                    state, total_count
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'QUEUED',%s
                )
                RETURNING *
                """,
                (
                    batch_id,
                    discovery_id,
                    discovery["telegram_user_id"],
                    discovery["telegram_chat_id"],
                    discovery["telegram_message_id"],
                    discovery["platform"],
                    discovery["author_id"],
                    discovery["canonical_author_url"],
                    discovery["author_name"],
                    Jsonb({
                        "version": 1,
                        "kind": selection_kind,
                        "limit": len(selected),
                        "preview_eligible_count": discovery["eligible_count"],
                        "estimated_duration_seconds": estimated_seconds,
                        "duration_budget_enforced":
                            self.settings.enforce_batch_estimated_duration_budget,
                    }),
                    destination_value,
                    len(selected),
                ),
            ).fetchone()

            for position, work in enumerate(selected, start=1):
                document = self._find_completed_document(
                    conn,
                    platform=work["platform"],
                    source_id=work["source_id"],
                    canonical_url=work["canonical_url"],
                    destination=destination_value,
                )
                item_id = uuid.uuid5(
                    batch_id, f"item:{work['platform']}:{work['source_id']}"
                )
                if document:
                    conn.execute(
                        """
                        INSERT INTO video_ingestion_batch_items (
                            id, batch_id, platform, source_id, canonical_url,
                            title, published_at, duration_seconds, content_type,
                            position, state, existing_document_record_id,
                            finished_at
                        ) VALUES (
                            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                            'SKIPPED_EXISTING',%s,now()
                        )
                        """,
                        (
                            item_id,
                            batch_id,
                            work["platform"],
                            work["source_id"],
                            work["canonical_url"],
                            work["title"],
                            work["published_at"],
                            work["duration_seconds"],
                            work["content_type"],
                            position,
                            document["record_id"],
                        ),
                    )
                    continue

                job_id = uuid.uuid5(
                    batch_id, f"{work['platform']}:{work['source_id']}"
                )
                conn.execute(
                    """
                    INSERT INTO video_ingestion_jobs (
                        id, telegram_user_id, telegram_chat_id,
                        telegram_message_id, input_kind, input_value,
                        forward_origin, state, next_attempt_at, source_platform,
                        source_id, canonical_url, title, author,
                        duration_seconds, metadata, selected_destination,
                        notification_mode, destination_locked
                    ) VALUES (
                        %s,%s,%s,%s,'url',%s,'{}'::jsonb,'RECEIVED',now(),
                        %s,%s,%s,%s,%s,%s,%s,%s,'BATCH_SILENT',true
                    )
                    """,
                    (
                        job_id,
                        discovery["telegram_user_id"],
                        discovery["telegram_chat_id"],
                        discovery["telegram_message_id"],
                        work["canonical_url"],
                        work["platform"],
                        work["source_id"],
                        work["canonical_url"],
                        work["title"],
                        discovery["author_name"],
                        work["duration_seconds"],
                        Jsonb(
                            {
                                "batch_id": str(batch_id),
                                "discovery_position": work["position"],
                            }
                        ),
                        destination_value,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO video_ingestion_batch_items (
                        id, batch_id, platform, source_id, canonical_url,
                        title, published_at, duration_seconds, content_type,
                        position, state, job_id, queued_at
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'QUEUED',%s,now()
                    )
                    """,
                    (
                        item_id,
                        batch_id,
                        work["platform"],
                        work["source_id"],
                        work["canonical_url"],
                        work["title"],
                        work["published_at"],
                        work["duration_seconds"],
                        work["content_type"],
                        position,
                        job_id,
                    ),
                )

            batch = self._recount_batch(conn, batch_id)
            conn.execute(
                """
                UPDATE video_author_discoveries
                SET state='CONSUMED', finished_at=now(), updated_at=now()
                WHERE id=%s
                """,
                (discovery_id,),
            )
            return dict(batch)

    def find_completed_document_for_destination(
        self,
        *,
        platform: str,
        source_id: str,
        canonical_url: str,
        destination: Destination | str,
    ) -> dict[str, Any] | None:
        destination_value = (
            destination.value if isinstance(destination, Destination) else str(destination)
        )
        with self.connection() as conn:
            row = self._find_completed_document(
                conn,
                platform=platform,
                source_id=source_id,
                canonical_url=canonical_url,
                destination=destination_value,
            )
        return dict(row) if row else None

    def validate_discovery_selection(
        self,
        discovery_id: str | uuid.UUID,
        user_id: str,
        limit: int,
        selection_kind: str = "latest",
    ) -> dict[str, Any]:
        if limit < 1 or (
            selection_kind != "all_preview"
            and limit > self.settings.author_batch_max_items
        ):
            raise ValueError("batch limit is outside the configured range")
        if selection_kind not in {"latest", "all_preview"}:
            raise ValueError("unsupported selection kind")
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM video_author_discoveries
                WHERE id=%s AND telegram_user_id=%s
                """,
                (discovery_id, str(user_id)),
            ).fetchone()
            if not row:
                raise BatchNotFoundError(f"discovery not found: {discovery_id}")
            if row["state"] != "READY":
                raise BatchStateError(
                    f"discovery cannot be selected from {row['state']}"
                )
            now = conn.execute("SELECT now() AS value").fetchone()["value"]
            if row["expires_at"] <= now:
                raise BatchStateError("discovery has expired")
            if (
                selection_kind != "all_preview"
                and limit > author_batch_limit(self.settings, row["platform"])
            ):
                raise ValueError("batch limit exceeds the platform safety limit")
            if limit > row["eligible_count"]:
                raise ValueError("limit exceeds eligible discovery items")
            if (
                selection_kind == "all_preview"
                and limit != row["eligible_count"]
            ):
                raise ValueError(
                    "all-preview selection must include every eligible preview item"
                )
        return dict(row)

    def get_owned_batch(
        self,
        batch_id: str | uuid.UUID,
        user_id: str,
    ) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM video_ingestion_batches
                WHERE id=%s AND telegram_user_id=%s
                """,
                (batch_id, str(user_id)),
            ).fetchone()
            row = self._batch_with_current_work(conn, row) if row else None
        return dict(row) if row else None

    def latest_batch(self, user_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM video_ingestion_batches
                WHERE telegram_user_id=%s
                ORDER BY
                    (state=ANY(%s)) DESC,
                    created_at DESC
                LIMIT 1
                """,
                (str(user_id), list(ACTIVE_BATCH_STATES)),
            ).fetchone()
            row = self._batch_with_current_work(conn, row) if row else None
        return dict(row) if row else None

    def pause_latest_batch(self, user_id: str) -> dict[str, Any]:
        batch = self.latest_batch(user_id)
        if not batch:
            raise BatchNotFoundError("batch not found")
        return self.pause_batch(batch["id"], user_id)

    def resume_latest_batch(self, user_id: str) -> dict[str, Any]:
        batch = self.latest_batch(user_id)
        if not batch:
            raise BatchNotFoundError("batch not found")
        return self.resume_batch(batch["id"], user_id)

    def cancel_latest_batch(self, user_id: str) -> dict[str, Any]:
        batch = self.latest_batch(user_id)
        if not batch:
            raise BatchNotFoundError("batch not found")
        return self.request_batch_cancel(batch["id"], user_id)

    def batch_context_for_job(
        self, job_id: str | uuid.UUID
    ) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT batch.*, item.id AS item_id, item.state AS item_state
                FROM video_ingestion_batch_items item
                JOIN video_ingestion_batches batch ON batch.id=item.batch_id
                WHERE item.job_id=%s
                """,
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def skip_batch_child_existing(
        self,
        job_id: str | uuid.UUID,
        document_record_id: int,
    ) -> dict[str, Any]:
        with self.connection() as conn:
            batch, item, job = self._lock_child(conn, job_id)
            if item["state"] in TERMINAL_ITEM_STATES:
                return self._child_result(batch, item, job)
            if item["state"] != "RUNNING":
                raise BatchStateError(
                    f"cannot skip batch child from {item['state']}"
                )
            job = conn.execute(
                """
                UPDATE video_ingestion_jobs
                SET state='CANCELLED', processing_finished_at=now(),
                    last_error_code='SKIPPED_EXISTING',
                    last_error_message='Completed destination document found',
                    updated_at=now()
                WHERE id=%s RETURNING *
                """,
                (job_id,),
            ).fetchone()
            item = conn.execute(
                """
                UPDATE video_ingestion_batch_items
                SET state='SKIPPED_EXISTING', job_id=NULL,
                    existing_document_record_id=%s, finished_at=now(),
                    error_code=NULL, error_message=NULL, updated_at=now()
                WHERE id=%s RETURNING *
                """,
                (document_record_id, item["id"]),
            ).fetchone()
            batch = self._recount_batch(conn, batch["id"])
            return self._child_result(batch, item, job)

    def pause_and_requeue_batch_child(
        self,
        job_id: str | uuid.UUID,
        *,
        state: BatchState,
        error_code: str,
        error_message: str,
        next_attempt_at: datetime,
    ) -> dict[str, Any]:
        if state.value not in PAUSED_BATCH_STATES:
            raise ValueError("target state is not a paused batch state")
        with self.connection() as conn:
            batch, item, job = self._lock_child(conn, job_id)
            if item["state"] in TERMINAL_ITEM_STATES:
                return self._child_result(batch, item, job)
            if item["state"] != "RUNNING":
                raise BatchStateError(
                    f"cannot pause batch child from {item['state']}"
                )
            job = conn.execute(
                """
                UPDATE video_ingestion_jobs
                SET state='RECEIVED', retry_count=retry_count+1,
                    next_attempt_at=%s, processing_started_at=NULL,
                    last_error_code=%s, last_error_message=%s, updated_at=now()
                WHERE id=%s RETURNING *
                """,
                (next_attempt_at, error_code, error_message, job_id),
            ).fetchone()
            item = conn.execute(
                """
                UPDATE video_ingestion_batch_items
                SET state='QUEUED', started_at=NULL, finished_at=NULL,
                    error_code=%s, error_message=%s, updated_at=now()
                WHERE id=%s RETURNING *
                """,
                (error_code, error_message, item["id"]),
            ).fetchone()
            batch = conn.execute(
                """
                UPDATE video_ingestion_batches
                SET state=%s, pause_code=%s, pause_message=%s,
                    resume_not_before=%s, updated_at=now()
                WHERE id=%s RETURNING *
                """,
                (
                    state.value,
                    error_code,
                    error_message,
                    next_attempt_at,
                    batch["id"],
                ),
            ).fetchone()
            batch = self._recount_batch(conn, batch["id"])
            return self._child_result(batch, item, job)

    def record_batch_child_duration(
        self,
        job_id: str | uuid.UUID,
        duration_seconds: float | None,
    ) -> bool:
        if duration_seconds is None:
            return True
        with self.connection() as conn:
            batch, item, _job = self._lock_child(conn, job_id)
            estimate = conn.execute(
                """
                SELECT sum(
                    CASE WHEN id=%s THEN %s
                         WHEN duration_seconds IS NULL THEN %s
                         ELSE duration_seconds END
                ) AS seconds
                FROM video_ingestion_batch_items
                WHERE batch_id=%s
                """,
                (
                    item["id"],
                    duration_seconds,
                    self.settings.max_batch_unknown_duration_reservation_seconds,
                    batch["id"],
                ),
            ).fetchone()["seconds"]
            if (
                not self.settings.enforce_batch_estimated_duration_budget
                or estimate
                <= self.settings.max_batch_estimated_duration_seconds
            ):
                conn.execute(
                    """
                    UPDATE video_ingestion_batch_items
                    SET duration_seconds=%s, updated_at=now() WHERE id=%s
                    """,
                    (duration_seconds, item["id"]),
                )
                return True
            conn.execute(
                """
                UPDATE video_ingestion_jobs
                SET state='FAILED', processing_finished_at=now(),
                    last_error_code='BATCH_DURATION_LIMIT',
                    last_error_message='Accurate duration exceeds batch budget',
                    updated_at=now()
                WHERE id=%s
                """,
                (job_id,),
            )
            conn.execute(
                """
                UPDATE video_ingestion_batch_items
                SET state='FAILED', duration_seconds=%s, finished_at=now(),
                    error_code='BATCH_DURATION_LIMIT',
                    error_message='Accurate duration exceeds batch budget',
                    updated_at=now()
                WHERE id=%s
                """,
                (duration_seconds, item["id"]),
            )
            self._recount_batch(conn, batch["id"])
            return False

    def queue_batch_child_persistence(
        self,
        job_id: str | uuid.UUID,
    ) -> dict[str, Any]:
        """Return a staged batch child to the queue for its persistence phase."""
        with self.connection() as conn:
            batch, item, job = self._lock_child(conn, job_id)
            if item["state"] in TERMINAL_ITEM_STATES:
                return self._child_result(batch, item, job)
            if job["state"] != "PERSISTING":
                raise BatchStateError(
                    f"cannot queue persistence from job state {job['state']}"
                )
            if item["state"] not in {"QUEUED", "RUNNING"}:
                raise BatchStateError(
                    f"cannot queue persistence from item state {item['state']}"
                )
            item = conn.execute(
                """
                UPDATE video_ingestion_batch_items
                SET state='QUEUED', queued_at=now(),
                    error_code=NULL, error_message=NULL, updated_at=now()
                WHERE id=%s
                RETURNING *
                """,
                (item["id"],),
            ).fetchone()
            batch = self._recount_batch(conn, batch["id"])
            return self._child_result(batch, item, job)

    @staticmethod
    def _find_completed_document(
        conn: Any,
        *,
        platform: str,
        source_id: str,
        canonical_url: str,
        destination: str,
    ) -> dict[str, Any] | None:
        return conn.execute(
            """
            SELECT record_id FROM video_knowledge_documents
            WHERE selected_knowledge_base=%s
              AND ingestion_status='completed'
              AND (
                    (source_platform=%s AND source_id=%s)
                    OR source_url=%s
              )
            ORDER BY
                ((source_platform=%s AND source_id=%s)) DESC,
                updated_at DESC
            LIMIT 1
            """,
            (
                destination,
                platform,
                source_id,
                canonical_url,
                platform,
                source_id,
            ),
        ).fetchone()

    def pause_batch(
        self,
        batch_id: str | uuid.UUID,
        user_id: str,
        *,
        state: BatchState = BatchState.PAUSED_USER,
        code: str | None = None,
        message: str | None = None,
        resume_not_before: datetime | None = None,
    ) -> dict[str, Any]:
        if state.value not in PAUSED_BATCH_STATES:
            raise ValueError("target state is not a paused batch state")
        with self.connection() as conn:
            batch = self._lock_owned_batch(conn, batch_id, user_id)
            if batch["state"] in PAUSED_BATCH_STATES:
                return dict(batch)
            if batch["state"] not in {"QUEUED", "RUNNING"}:
                raise BatchStateError(f"cannot pause batch from {batch['state']}")
            row = conn.execute(
                """
                UPDATE video_ingestion_batches
                SET state=%s, pause_code=%s, pause_message=%s,
                    resume_not_before=%s, updated_at=now()
                WHERE id=%s
                RETURNING *
                """,
                (state.value, code, message, resume_not_before, batch_id),
            ).fetchone()
            self._enqueue_batch_event(conn, row, "PAUSED")
            return dict(row)

    def resume_batch(
        self,
        batch_id: str | uuid.UUID,
        user_id: str,
        *,
        allowed_states: tuple[BatchState, ...] = (BatchState.PAUSED_USER,),
    ) -> dict[str, Any]:
        allowed = {state.value for state in allowed_states}
        if not allowed or not allowed <= set(PAUSED_BATCH_STATES):
            raise ValueError("allowed resume states must all be paused states")
        with self.connection() as conn:
            batch = self._lock_owned_batch(conn, batch_id, user_id)
            if batch["state"] in {"QUEUED", "RUNNING"}:
                return dict(batch)
            if batch["state"] not in allowed:
                raise BatchStateError(f"cannot resume batch from {batch['state']}")
            target = "RUNNING" if batch["started_at"] else "QUEUED"
            row = conn.execute(
                """
                UPDATE video_ingestion_batches
                SET state=%s, pause_code=NULL, pause_message=NULL,
                    resume_not_before=NULL, updated_at=now()
                WHERE id=%s
                RETURNING *
                """,
                (target, batch_id),
            ).fetchone()
            self._enqueue_batch_event(conn, row, "RESUMED")
            return dict(row)

    def resume_recovered_xiaohongshu_batches(self) -> int:
        """Resume only auth-paused XHS batches after a verified session probe."""
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO video_platform_request_gates (platform)
                VALUES ('xiaohongshu') ON CONFLICT (platform) DO NOTHING
                """
            )
            conn.execute(
                """
                UPDATE video_platform_request_gates
                SET circuit_state='CLOSED', blocked_until=NULL,
                    consecutive_failures=0, last_error_code=NULL,
                    probe_in_flight=false, opened_at=NULL,
                    next_allowed_at=now(), last_success_at=now(),
                    updated_at=now()
                WHERE platform='xiaohongshu'
                """
            )
            rows = conn.execute(
                """
                UPDATE video_ingestion_batches
                SET state=CASE WHEN started_at IS NULL
                               THEN 'QUEUED' ELSE 'RUNNING' END,
                    pause_code=NULL, pause_message=NULL,
                    resume_not_before=NULL, updated_at=now()
                WHERE platform='xiaohongshu'
                  AND state='PAUSED_AUTH'
                  AND pause_code='AUTH_EXPIRED'
                RETURNING *
                """
            ).fetchall()
            if rows:
                conn.execute(
                    """
                    UPDATE video_ingestion_jobs job
                    SET next_attempt_at=now(), updated_at=now()
                    FROM video_ingestion_batch_items item
                    WHERE item.batch_id=ANY(%s)
                      AND item.job_id=job.id
                      AND item.state IN ('PENDING','QUEUED')
                      AND job.state IN ('RECEIVED','PERSISTING')
                    """,
                    ([row["id"] for row in rows],),
                )
            for row in rows:
                self._enqueue_batch_event(
                    conn,
                    self._batch_with_current_work(conn, row),
                    "RESUMED",
                )
            return len(rows)

    def resume_recovered_bilibili_batches(self) -> int:
        """Resume only auth-paused Bilibili batches after a verified probe."""
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO video_platform_request_gates (platform)
                VALUES ('bilibili') ON CONFLICT (platform) DO NOTHING
                """
            )
            conn.execute(
                """
                UPDATE video_platform_request_gates
                SET circuit_state='CLOSED', blocked_until=NULL,
                    consecutive_failures=0, last_error_code=NULL,
                    probe_in_flight=false, opened_at=NULL,
                    next_allowed_at=now(), last_success_at=now(),
                    updated_at=now()
                WHERE platform='bilibili'
                """
            )
            rows = conn.execute(
                """
                UPDATE video_ingestion_batches
                SET state=CASE WHEN started_at IS NULL
                               THEN 'QUEUED' ELSE 'RUNNING' END,
                    pause_code=NULL, pause_message=NULL,
                    resume_not_before=NULL, updated_at=now()
                WHERE platform='bilibili'
                  AND state='PAUSED_AUTH'
                  AND pause_code='AUTH_EXPIRED'
                RETURNING *
                """
            ).fetchall()
            if rows:
                conn.execute(
                    """
                    UPDATE video_ingestion_jobs job
                    SET next_attempt_at=now(), updated_at=now()
                    FROM video_ingestion_batch_items item
                    WHERE item.batch_id=ANY(%s)
                      AND item.job_id=job.id
                      AND item.state IN ('PENDING','QUEUED')
                      AND job.state IN ('RECEIVED','PERSISTING')
                    """,
                    ([row["id"] for row in rows],),
                )
            for row in rows:
                self._enqueue_batch_event(
                    conn,
                    self._batch_with_current_work(conn, row),
                    "RESUMED",
                )
            return len(rows)

    def request_batch_cancel(
        self, batch_id: str | uuid.UUID, user_id: str
    ) -> dict[str, Any]:
        with self.connection() as conn:
            visible = conn.execute(
                "SELECT * FROM video_ingestion_batches WHERE id=%s",
                (batch_id,),
            ).fetchone()
            if not visible:
                raise BatchNotFoundError(f"batch not found: {batch_id}")
            if str(visible["telegram_user_id"]) != str(user_id):
                raise BatchForbiddenError("batch belongs to another user")
            # Use the same child -> job -> parent lock order as claim_next().
            # This prevents a claim/cancel deadlock while preserving one atomic
            # cancellation transaction.
            queued_items = conn.execute(
                """
                SELECT id, job_id FROM video_ingestion_batch_items
                WHERE batch_id=%s AND state IN ('PENDING','QUEUED')
                ORDER BY position
                FOR UPDATE
                """,
                (batch_id,),
            ).fetchall()
            job_ids = [row["job_id"] for row in queued_items if row["job_id"]]
            if job_ids:
                conn.execute(
                    """
                    SELECT id FROM video_ingestion_jobs
                    WHERE id=ANY(%s)
                    ORDER BY id
                    FOR UPDATE
                    """,
                    (job_ids,),
                ).fetchall()
            batch = self._lock_owned_batch(conn, batch_id, user_id)
            if batch["state"] not in ACTIVE_BATCH_STATES:
                return dict(batch)
            conn.execute(
                """
                UPDATE video_ingestion_batches
                SET state='CANCELLING',
                    cancel_requested_at=coalesce(cancel_requested_at,now()),
                    pause_code=NULL, pause_message=NULL,
                    resume_not_before=NULL, updated_at=now()
                WHERE id=%s
                """,
                (batch_id,),
            )
            queued_jobs = conn.execute(
                """
                UPDATE video_ingestion_jobs job
                SET state='CANCELLED', processing_finished_at=now(),
                    last_error_code='BATCH_CANCELLED',
                    last_error_message='Batch cancellation requested',
                    updated_at=now()
                FROM video_ingestion_batch_items item
                WHERE item.batch_id=%s
                  AND item.job_id=job.id
                  AND item.state IN ('PENDING','QUEUED')
                RETURNING job.id
                """,
                (batch_id,),
            ).fetchall()
            conn.execute(
                """
                UPDATE video_ingestion_batch_items
                SET state='CANCELLED', finished_at=now(), updated_at=now(),
                    error_code='BATCH_CANCELLED',
                    error_message='Batch cancellation requested'
                WHERE batch_id=%s AND state IN ('PENDING','QUEUED')
                """,
                (batch_id,),
            )
            # PENDING items do not have jobs; the value is useful in structured logs.
            _ = queued_jobs
            return dict(self._recount_batch(conn, batch_id))

    def complete_batch_child(
        self, job_id: str | uuid.UUID, document_id: str | uuid.UUID
    ) -> dict[str, Any]:
        with self.connection() as conn:
            batch, item, job = self._lock_child(conn, job_id)
            if item["state"] in TERMINAL_ITEM_STATES:
                return self._child_result(batch, item, job)
            if item["state"] != "RUNNING":
                raise BatchStateError(f"cannot complete child from {item['state']}")
            job = conn.execute(
                """
                UPDATE video_ingestion_jobs
                SET state='COMPLETED', document_id=%s,
                    processing_finished_at=now(), updated_at=now()
                WHERE id=%s
                RETURNING *
                """,
                (document_id, job_id),
            ).fetchone()
            item = conn.execute(
                """
                UPDATE video_ingestion_batch_items
                SET state='COMPLETED', finished_at=now(), updated_at=now(),
                    error_code=NULL, error_message=NULL
                WHERE job_id=%s
                RETURNING *
                """,
                (job_id,),
            ).fetchone()
            batch = self._recount_batch(conn, batch["id"])
            return self._child_result(batch, item, job)

    def retry_batch_child(
        self,
        job_id: str | uuid.UUID,
        error_code: str,
        error_message: str,
        next_attempt_at: datetime,
        *,
        increment_retry: bool = True,
    ) -> dict[str, Any]:
        with self.connection() as conn:
            batch, item, job = self._lock_child(conn, job_id)
            if item["state"] in TERMINAL_ITEM_STATES:
                return self._child_result(batch, item, job)
            if item["state"] != "RUNNING":
                raise BatchStateError(f"cannot retry child from {item['state']}")
            job = conn.execute(
                """
                UPDATE video_ingestion_jobs
                SET state='RECEIVED',
                    retry_count=retry_count + CASE WHEN %s THEN 1 ELSE 0 END,
                    next_attempt_at=%s, processing_started_at=NULL,
                    last_error_code=%s, last_error_message=%s, updated_at=now()
                WHERE id=%s
                RETURNING *
                """,
                (
                    increment_retry,
                    next_attempt_at,
                    error_code,
                    error_message,
                    job_id,
                ),
            ).fetchone()
            item = conn.execute(
                """
                UPDATE video_ingestion_batch_items
                SET state='QUEUED', started_at=NULL, finished_at=NULL,
                    error_code=%s, error_message=%s, updated_at=now()
                WHERE job_id=%s
                RETURNING *
                """,
                (error_code, error_message, job_id),
            ).fetchone()
            batch = self._recount_batch(conn, batch["id"])
            return self._child_result(batch, item, job)

    def fail_batch_child(
        self,
        job_id: str | uuid.UUID,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any]:
        return self._finish_batch_child(
            job_id,
            item_state="FAILED",
            job_state="FAILED",
            error_code=error_code,
            error_message=error_message,
        )

    def cancel_batch_child(
        self,
        job_id: str | uuid.UUID,
        error_code: str = "BATCH_CANCELLED",
        error_message: str = "Batch cancellation requested",
    ) -> dict[str, Any]:
        return self._finish_batch_child(
            job_id,
            item_state="CANCELLED",
            job_state="CANCELLED",
            error_code=error_code,
            error_message=error_message,
        )

    def _finish_batch_child(
        self,
        job_id: str | uuid.UUID,
        *,
        item_state: str,
        job_state: str,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any]:
        with self.connection() as conn:
            batch, item, job = self._lock_child(conn, job_id)
            if item["state"] in TERMINAL_ITEM_STATES:
                return self._child_result(batch, item, job)
            if item["state"] not in {"QUEUED", "RUNNING"}:
                raise BatchStateError(
                    f"cannot finish child from {item['state']}"
                )
            job = conn.execute(
                """
                UPDATE video_ingestion_jobs
                SET state=%s, processing_finished_at=now(),
                    last_error_code=%s, last_error_message=%s, updated_at=now()
                WHERE id=%s
                RETURNING *
                """,
                (job_state, error_code, error_message, job_id),
            ).fetchone()
            item = conn.execute(
                """
                UPDATE video_ingestion_batch_items
                SET state=%s, finished_at=now(), updated_at=now(),
                    error_code=%s, error_message=%s
                WHERE job_id=%s
                RETURNING *
                """,
                (item_state, error_code, error_message, job_id),
            ).fetchone()
            batch = self._recount_batch(conn, batch["id"])
            return self._child_result(batch, item, job)

    def reconcile_batches(self) -> dict[str, int]:
        """Repair item mirrors and cached counts after a worker restart."""
        with self.connection() as conn:
            changed = conn.execute(
                """
                WITH repaired AS (
                    UPDATE video_ingestion_batch_items item
                    SET state=CASE
                            WHEN job.state='COMPLETED' THEN 'COMPLETED'
                            WHEN job.state='FAILED' THEN 'FAILED'
                            WHEN job.state='CANCELLED' THEN 'CANCELLED'
                            ELSE 'QUEUED'
                        END,
                        started_at=CASE
                            WHEN job.state IN ('COMPLETED','FAILED','CANCELLED')
                                THEN item.started_at
                            ELSE NULL
                        END,
                        finished_at=CASE
                            WHEN job.state IN ('COMPLETED','FAILED','CANCELLED')
                                THEN coalesce(item.finished_at,job.processing_finished_at,now())
                            ELSE NULL
                        END,
                        error_code=job.last_error_code,
                        error_message=job.last_error_message,
                        updated_at=now()
                    FROM video_ingestion_jobs job
                    WHERE item.job_id=job.id
                      AND (
                            (item.state='RUNNING' AND job.state IN ('RECEIVED','PERSISTING'))
                            OR (item.state NOT IN ('COMPLETED','FAILED','CANCELLED')
                                AND job.state IN ('COMPLETED','FAILED','CANCELLED'))
                      )
                    RETURNING item.id, item.batch_id
                )
                SELECT id, batch_id FROM repaired
                """
            ).fetchall()
            batch_ids = {
                row["batch_id"] for row in changed
            } | {
                row["id"]
                for row in conn.execute(
                    """
                    SELECT id FROM video_ingestion_batches
                    WHERE state=ANY(%s)
                    """,
                    (list(ACTIVE_BATCH_STATES),),
                ).fetchall()
            }
            for batch_id in sorted(batch_ids, key=str):
                conn.execute(
                    "SELECT id FROM video_ingestion_batches WHERE id=%s FOR UPDATE",
                    (batch_id,),
                )
                self._recount_batch(conn, batch_id)
            expired = conn.execute(
                """
                UPDATE video_author_discoveries
                SET state='EXPIRED', finished_at=now(), updated_at=now()
                WHERE state='READY' AND expires_at <= now()
                RETURNING id
                """
            ).fetchall()
            return {
                "repaired_items": len(changed),
                "recounted_batches": len(batch_ids),
                "expired_discoveries": len(expired),
            }

    @staticmethod
    def _lock_owned_batch(
        conn: Any, batch_id: str | uuid.UUID, user_id: str
    ) -> dict[str, Any]:
        batch = conn.execute(
            "SELECT * FROM video_ingestion_batches WHERE id=%s FOR UPDATE",
            (batch_id,),
        ).fetchone()
        if not batch:
            raise BatchNotFoundError(f"batch not found: {batch_id}")
        if str(batch["telegram_user_id"]) != str(user_id):
            raise BatchForbiddenError("batch belongs to another user")
        return batch

    @staticmethod
    def _lock_child(
        conn: Any, job_id: str | uuid.UUID
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        link = conn.execute(
            """
            SELECT batch_id FROM video_ingestion_batch_items
            WHERE job_id=%s
            """,
            (job_id,),
        ).fetchone()
        if not link:
            raise BatchNotFoundError(f"batch child not found: {job_id}")
        batch = conn.execute(
            "SELECT * FROM video_ingestion_batches WHERE id=%s FOR UPDATE",
            (link["batch_id"],),
        ).fetchone()
        item = conn.execute(
            """
            SELECT * FROM video_ingestion_batch_items
            WHERE job_id=%s
            FOR UPDATE
            """,
            (job_id,),
        ).fetchone()
        job = conn.execute(
            "SELECT * FROM video_ingestion_jobs WHERE id=%s FOR UPDATE",
            (job_id,),
        ).fetchone()
        if not item or not job:
            raise BatchRepositoryError(f"broken batch child association: {job_id}")
        return batch, item, job

    @staticmethod
    def _child_result(
        batch: dict[str, Any], item: dict[str, Any], job: dict[str, Any]
    ) -> dict[str, Any]:
        return {"batch": dict(batch), "item": dict(item), "job": dict(job)}

    def _recount_batch(
        self, conn: Any, batch_id: str | uuid.UUID
    ) -> dict[str, Any]:
        batch = conn.execute(
            "SELECT * FROM video_ingestion_batches WHERE id=%s FOR UPDATE",
            (batch_id,),
        ).fetchone()
        if not batch:
            raise BatchNotFoundError(f"batch not found: {batch_id}")
        counts = conn.execute(
            """
            SELECT
                count(*) AS actual_total,
                count(*) FILTER (WHERE state IN ('PENDING','QUEUED')) AS queued,
                count(*) FILTER (WHERE state='RUNNING') AS running,
                count(*) FILTER (WHERE state='COMPLETED') AS completed,
                count(*) FILTER (WHERE state='SKIPPED_EXISTING') AS skipped,
                count(*) FILTER (WHERE state='FAILED') AS failed,
                count(*) FILTER (WHERE state='CANCELLED') AS cancelled,
                count(*) FILTER (WHERE state=ANY(%s)) AS terminal
            FROM video_ingestion_batch_items
            WHERE batch_id=%s
            """,
            (list(TERMINAL_ITEM_STATES), batch_id),
        ).fetchone()
        state = batch["state"]
        finished_at = batch["finished_at"]
        if counts["actual_total"] != batch["total_count"]:
            state = "FAILED"
            finished_at = finished_at or conn.execute(
                "SELECT now() AS value"
            ).fetchone()["value"]
            pause_code = "ITEM_COUNT_MISMATCH"
            pause_message = (
                f"expected {batch['total_count']} items, "
                f"found {counts['actual_total']}"
            )
        else:
            pause_code = batch["pause_code"]
            pause_message = batch["pause_message"]
            if (
                state not in {
                    "COMPLETED",
                    "COMPLETED_WITH_ERRORS",
                    "CANCELLED",
                    "FAILED",
                }
                and counts["terminal"] == batch["total_count"]
            ):
                if counts["cancelled"] > 0:
                    state = "CANCELLED"
                elif counts["failed"] > 0:
                    state = "COMPLETED_WITH_ERRORS"
                else:
                    state = "COMPLETED"
                finished_at = conn.execute(
                    "SELECT now() AS value"
                ).fetchone()["value"]

        row = conn.execute(
            """
            UPDATE video_ingestion_batches
            SET queued_count=%s, running_count=%s, completed_count=%s,
                skipped_existing_count=%s, failed_count=%s,
                cancelled_count=%s, state=%s, finished_at=%s,
                pause_code=%s, pause_message=%s, updated_at=now()
            WHERE id=%s
            RETURNING *
            """,
            (
                counts["queued"],
                counts["running"],
                counts["completed"],
                counts["skipped"],
                counts["failed"],
                counts["cancelled"],
                state,
                finished_at,
                pause_code,
                pause_message,
                batch_id,
            ),
        ).fetchone()
        enriched = self._batch_with_current_work(conn, row)
        self._maybe_enqueue_batch_event(conn, enriched)
        return enriched

    @staticmethod
    def _batch_with_current_work(
        conn: Any, batch: dict[str, Any]
    ) -> dict[str, Any]:
        enriched = dict(batch)
        progress = conn.execute(
            """
            SELECT current.current_item_id,
                   current.current_title,
                   current.current_job_id,
                   current.current_job_state,
                   counts.started_count
            FROM (
                SELECT count(*) FILTER (
                    WHERE started_at IS NOT NULL OR state=ANY(%s)
                ) AS started_count
                FROM video_ingestion_batch_items
                WHERE batch_id=%s
            ) counts
            LEFT JOIN LATERAL (
                SELECT item.id AS current_item_id,
                       coalesce(job.title, item.title) AS current_title,
                       job.id AS current_job_id,
                       job.state AS current_job_state
                FROM video_ingestion_batch_items item
                LEFT JOIN video_ingestion_jobs job ON job.id=item.job_id
                WHERE item.batch_id=%s AND item.state='RUNNING'
                ORDER BY item.position
                LIMIT 1
            ) current ON true
            """,
            (
                list(TERMINAL_ITEM_STATES),
                batch["id"],
                batch["id"],
            ),
        ).fetchone()
        enriched.update(dict(progress) if progress else {
            "current_item_id": None,
            "current_title": None,
            "current_job_id": None,
            "current_job_state": None,
            "started_count": 0,
        })
        return enriched

    def _maybe_enqueue_batch_event(self, conn: Any, batch: dict[str, Any]) -> None:
        existing = conn.execute(
            """
            SELECT 1 FROM video_ingestion_batch_notifications
            WHERE batch_id=%s LIMIT 1
            """,
            (batch["id"],),
        ).fetchone()
        terminal_count = (
            batch["completed_count"]
            + batch["skipped_existing_count"]
            + batch["failed_count"]
            + batch["cancelled_count"]
        )
        event_type = None
        if not existing:
            event_type = "CREATED"
        elif batch["state"] in {
            "COMPLETED",
            "COMPLETED_WITH_ERRORS",
            "CANCELLED",
            "FAILED",
        }:
            event_type = "TERMINAL"
        elif batch["state"].startswith("PAUSED_"):
            event_type = "PAUSED"
        elif batch["state"] == "CANCELLING":
            event_type = "CANCELLING"
        elif (
            terminal_count - batch["notified_terminal_count"]
            >= self.settings.batch_notification_item_threshold
            or batch["last_notified_at"] is None
            or batch["last_notified_at"]
            <= conn.execute(
                "SELECT now() - make_interval(secs => %s) AS value",
                (self.settings.batch_notification_interval_seconds,),
            ).fetchone()["value"]
        ):
            event_type = "PROGRESS"
        if event_type:
            self._enqueue_batch_event(conn, batch, event_type)

    def _enqueue_batch_event(
        self,
        conn: Any,
        batch: dict[str, Any],
        event_type: str,
    ) -> None:
        terminal_count = (
            batch["completed_count"]
            + batch["skipped_existing_count"]
            + batch["failed_count"]
            + batch["cancelled_count"]
        )
        work_key = ""
        if event_type == "PROGRESS":
            work_key = (
                f":{batch.get('current_item_id') or ''}:"
                f"{video_stage_label(batch.get('current_job_state'), batch['state'])}"
            )
        key = (
            f"{batch['id']}:{event_type}:{batch['state']}:{terminal_count}"
            f"{work_key}"
        )
        inserted = conn.execute(
            """
            INSERT INTO video_ingestion_batch_notifications (
                id, batch_id, event_type, idempotency_key, payload
            ) VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            (
                uuid.uuid5(uuid.NAMESPACE_URL, f"video-batch-notification:{key}"),
                batch["id"],
                event_type,
                key,
                Jsonb({
                    "chat_id": str(batch["telegram_chat_id"]),
                    "text": batch_text(batch),
                    "batch_id": str(batch["id"]),
                    "state": batch["state"],
                    "terminal_count": terminal_count,
                }),
            ),
        ).fetchone()
        if inserted:
            conn.execute(
                """
                UPDATE video_ingestion_batches
                SET last_notified_at=now(), notified_terminal_count=%s
                WHERE id=%s
                """,
                (terminal_count, batch["id"]),
            )

    def claim_batch_notification(self) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                WITH candidate AS (
                    SELECT id FROM video_ingestion_batch_notifications
                    WHERE delivered_at IS NULL AND next_attempt_at <= now()
                      AND attempts < 100
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE video_ingestion_batch_notifications notification
                SET attempts=attempts+1,
                    next_attempt_at=now() + make_interval(
                        secs => least(300, power(2, attempts)::integer)
                    )
                FROM candidate
                WHERE notification.id=candidate.id
                RETURNING notification.*
                """
            ).fetchone()
        return dict(row) if row else None

    def latest_batch_notification_message_id(
        self, batch_id: str | uuid.UUID
    ) -> int | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT payload->>'telegram_message_id' AS message_id
                FROM video_ingestion_batch_notifications
                WHERE batch_id=%s AND delivered_at IS NOT NULL
                  AND payload ? 'telegram_message_id'
                ORDER BY delivered_at DESC
                LIMIT 1
                """,
                (batch_id,),
            ).fetchone()
        return int(row["message_id"]) if row and row["message_id"] else None

    def mark_batch_notification_delivered(
        self,
        notification_id: str | uuid.UUID,
        telegram_message_id: int | None = None,
    ) -> None:
        with self.connection() as conn:
            row = conn.execute(
                """
                UPDATE video_ingestion_batch_notifications
                SET delivered_at=now(),
                    payload=CASE
                        WHEN %s::bigint IS NULL THEN payload
                        ELSE payload || jsonb_build_object(
                            'telegram_message_id', %s::bigint
                        )
                    END
                WHERE id=%s AND delivered_at IS NULL
                RETURNING id
                """,
                (
                    telegram_message_id,
                    telegram_message_id,
                    notification_id,
                ),
            ).fetchone()
        if not row:
            raise KeyError(notification_id)

    def batch_metrics(self) -> dict[str, Any]:
        with self.connection() as conn:
            batch_states = conn.execute(
                """
                SELECT state, count(*) AS count
                FROM video_ingestion_batches GROUP BY state
                """
            ).fetchall()
            outbox = conn.execute(
                """
                SELECT count(*) AS pending,
                    extract(epoch FROM now()-min(created_at)) AS oldest_seconds
                FROM video_ingestion_batch_notifications
                WHERE delivered_at IS NULL
                """
            ).fetchone()
            circuits = conn.execute(
                """
                SELECT platform, circuit_state, last_error_code
                FROM video_platform_request_gates
                WHERE circuit_state <> 'CLOSED'
                ORDER BY platform
                """
            ).fetchall()
            audit = self._batch_consistency_rows(conn)
        return {
            "batch_states": {row["state"]: row["count"] for row in batch_states},
            "outbox_pending": outbox["pending"],
            "outbox_oldest_seconds": (
                float(outbox["oldest_seconds"])
                if outbox["oldest_seconds"] is not None else 0
            ),
            "open_circuits": [dict(row) for row in circuits],
            "consistency_issue_count": len(audit),
        }

    def audit_batch_consistency(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [
                dict(row) for row in self._batch_consistency_rows(conn)
            ]

    @staticmethod
    def _batch_consistency_rows(conn: Any) -> list[dict[str, Any]]:
        return conn.execute(
            """
            WITH actual AS (
                SELECT batch_id,
                    count(*) AS total_count,
                    count(*) FILTER (WHERE state IN ('PENDING','QUEUED')) AS queued_count,
                    count(*) FILTER (WHERE state='RUNNING') AS running_count,
                    count(*) FILTER (WHERE state='COMPLETED') AS completed_count,
                    count(*) FILTER (WHERE state='SKIPPED_EXISTING') AS skipped_existing_count,
                    count(*) FILTER (WHERE state='FAILED') AS failed_count,
                    count(*) FILTER (WHERE state='CANCELLED') AS cancelled_count
                FROM video_ingestion_batch_items GROUP BY batch_id
            )
            SELECT batch.id AS batch_id, 'COUNT_DRIFT' AS issue
            FROM video_ingestion_batches batch
            JOIN actual ON actual.batch_id=batch.id
            WHERE (batch.total_count, batch.queued_count, batch.running_count,
                   batch.completed_count, batch.skipped_existing_count,
                   batch.failed_count, batch.cancelled_count)
               <> (actual.total_count, actual.queued_count, actual.running_count,
                   actual.completed_count, actual.skipped_existing_count,
                   actual.failed_count, actual.cancelled_count)
            UNION ALL
            SELECT item.id, 'ORPHAN_ITEM_JOB'
            FROM video_ingestion_batch_items item
            LEFT JOIN video_ingestion_jobs job ON job.id=item.job_id
            WHERE item.job_id IS NOT NULL AND job.id IS NULL
            UNION ALL
            SELECT job.id, 'ORPHAN_BATCH_JOB'
            FROM video_ingestion_jobs job
            LEFT JOIN video_ingestion_batch_items item ON item.job_id=job.id
            WHERE job.notification_mode='BATCH_SILENT' AND item.id IS NULL
              AND job.last_error_code <> 'SKIPPED_EXISTING'
            ORDER BY 2, 1
            """
        ).fetchall()

    def pause_all_batches(self, code: str = "OPERATOR_PAUSE") -> int:
        with self.connection() as conn:
            rows = conn.execute(
                """
                UPDATE video_ingestion_batches
                SET state='PAUSED_USER', pause_code=%s,
                    pause_message='Operator emergency pause', updated_at=now()
                WHERE state IN ('QUEUED','RUNNING')
                RETURNING *
                """,
                (code,),
            ).fetchall()
            for row in rows:
                self._enqueue_batch_event(conn, row, "PAUSED")
        return len(rows)
