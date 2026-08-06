from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .batch_repository import BatchRepositoryMixin
from .config import Settings
from .discovery_repository import DiscoveryRepositoryMixin
from .models import SELECTABLE_DESTINATIONS, Destination, JobState, utc_now
from .platform_gate import PlatformGateMixin
from .product_config import product_config
from rag_favorite.config import ConfigError, read_env_file


class JobControlRequested(RuntimeError):
    """An in-flight job was paused or cancelled by its owner."""

    def __init__(self, state: str):
        super().__init__(state)
        self.state = state


class SqlRepository(
    BatchRepositoryMixin,
    DiscoveryRepositoryMixin,
    PlatformGateMixin,
):
    def __init__(
        self,
        settings: Settings,
        connection_factory: Callable[[], psycopg.Connection] | None = None,
    ):
        self.settings = settings
        self._connection_factory = connection_factory

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        if self._connection_factory is not None:
            with self._connection_factory() as connection:
                yield connection
            return
        config = product_config()
        values = read_env_file(self.settings.database_env)
        database = (
            values.get("RAG_DATABASE_NAME")
            or values.get("PGDATABASE")
            or values.get("POSTGRES_DB")
            or config.database.name
        )
        user = (
            values.get("RAG_DATABASE_USER")
            or values.get("PGUSER")
            or values.get("POSTGRES_USER")
            or config.database.user
        )
        password = (
            values.get("RAG_DATABASE_PASSWORD")
            or values.get("PGPASSWORD")
            or values.get("POSTGRES_PASSWORD")
        )
        if not password:
            raise ConfigError("Ingestion database credential is incomplete.")
        with psycopg.connect(
            host=config.database.host,
            port=config.database.port,
            dbname=database,
            user=user,
            password=password,
            connect_timeout=10,
            row_factory=dict_row,
        ) as connection:
            yield connection

    def create_job(
        self,
        *,
        user_id: str,
        chat_id: str,
        message_id: str,
        input_kind: str,
        input_value: str,
        media_type: str | None = None,
        caption: str | None = None,
        forward_origin: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        source_platform: str | None = None,
        source_id: str | None = None,
        canonical_url: str | None = None,
        await_destination: bool = False,
    ) -> dict[str, Any]:
        job_id = uuid.uuid4()
        initial_state = (
            JobState.AWAITING_DESTINATION
            if await_destination
            else JobState.RECEIVED
        )
        with self.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO video_ingestion_jobs (
                    id, telegram_user_id, telegram_chat_id, telegram_message_id,
                    input_kind, input_value, media_type, caption, forward_origin,
                    metadata, source_platform, source_id, canonical_url,
                    state, next_attempt_at
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s
                )
                RETURNING *
                """,
                (
                    job_id, user_id, chat_id, message_id, input_kind, input_value,
                    media_type, caption, Jsonb(forward_origin or {}),
                    Jsonb(metadata or {}), source_platform, source_id,
                    canonical_url, initial_state.value,
                    None if await_destination else utc_now(),
                ),
            ).fetchone()
        return dict(row)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM video_ingestion_jobs WHERE id=%s", (job_id,)).fetchone()
        return dict(row) if row else None

    def resume_recovered_xiaohongshu_jobs(self) -> int:
        """Wake individual XHS jobs deferred solely for expired authentication."""
        with self.connection() as conn:
            rows = conn.execute(
                """
                UPDATE video_ingestion_jobs
                SET next_attempt_at=now(), processing_started_at=NULL,
                    last_error_code=NULL, last_error_message=NULL,
                    updated_at=now()
                WHERE notification_mode='INDIVIDUAL'
                  AND source_platform='xiaohongshu'
                  AND state='RECEIVED'
                  AND last_error_code='AUTH_EXPIRED'
                RETURNING id
                """
            ).fetchall()
        return len(rows)

    def resume_recovered_bilibili_jobs(self) -> int:
        """Wake individual Bilibili jobs after verified cookie recovery."""
        with self.connection() as conn:
            rows = conn.execute(
                """
                UPDATE video_ingestion_jobs
                SET next_attempt_at=now(), processing_started_at=NULL,
                    last_error_code=NULL, last_error_message=NULL,
                    updated_at=now()
                WHERE notification_mode='INDIVIDUAL'
                  AND source_platform='bilibili'
                  AND state='RECEIVED'
                  AND last_error_code='AUTH_EXPIRED'
                RETURNING id
                """
            ).fetchall()
        return len(rows)

    def latest_job(self, user_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT job.* FROM video_ingestion_jobs job
                WHERE job.telegram_user_id=%s
                  AND NOT EXISTS (
                      SELECT 1 FROM video_ingestion_batch_items item
                      WHERE item.job_id=job.id
                  )
                ORDER BY
                    (job.state=ANY(%s)) DESC,
                    job.created_at DESC
                LIMIT 1
                """,
                (
                    user_id,
                    [
                        "RECEIVED", "DOWNLOADING", "EXTRACTING_SUBTITLES",
                        "TRANSCRIBING", "BUILDING_MARKDOWN",
                        "ENRICHING_METADATA", "AWAITING_DESTINATION",
                        "PERSISTING", "PAUSED_USER",
                    ],
                ),
            ).fetchone()
        return dict(row) if row else None

    def get_owned_job(self, job_id: str, user_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT job.* FROM video_ingestion_jobs job
                WHERE job.id=%s AND job.telegram_user_id=%s
                  AND NOT EXISTS (
                      SELECT 1 FROM video_ingestion_batch_items item
                      WHERE item.job_id=job.id
                  )
                """,
                (job_id, str(user_id)),
            ).fetchone()
        return dict(row) if row else None

    def pause_job(self, job_id: str, user_id: str) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute(
                """
                UPDATE video_ingestion_jobs job
                SET state='PAUSED_USER', updated_at=now(),
                    last_error_code='USER_PAUSED',
                    last_error_message='Paused by user'
                WHERE job.id=%s AND job.telegram_user_id=%s
                  AND job.state IN (
                      'RECEIVED','DOWNLOADING','EXTRACTING_SUBTITLES',
                      'TRANSCRIBING','BUILDING_MARKDOWN',
                      'ENRICHING_METADATA','PERSISTING','PAUSED_USER'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM video_ingestion_batch_items item
                      WHERE item.job_id=job.id
                  )
                RETURNING job.*
                """,
                (job_id, str(user_id)),
            ).fetchone()
        if not row:
            raise ValueError("job cannot be paused")
        return dict(row)

    def resume_job(self, job_id: str, user_id: str) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute(
                """
                UPDATE video_ingestion_jobs job
                SET state=CASE WHEN staging_path IS NULL
                               THEN 'RECEIVED' ELSE 'PERSISTING' END,
                    next_attempt_at=now(), processing_started_at=NULL,
                    processing_finished_at=NULL, updated_at=now(),
                    last_error_code=NULL, last_error_message=NULL
                WHERE job.id=%s AND job.telegram_user_id=%s
                  AND job.state='PAUSED_USER'
                  AND NOT EXISTS (
                      SELECT 1 FROM video_ingestion_batch_items item
                      WHERE item.job_id=job.id
                  )
                RETURNING job.*
                """,
                (job_id, str(user_id)),
            ).fetchone()
        if not row:
            raise ValueError("job cannot be resumed")
        return dict(row)

    def cancel_job(self, job_id: str, user_id: str) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute(
                """
                UPDATE video_ingestion_jobs job
                SET state='CANCELLED', processing_finished_at=now(),
                    next_attempt_at=NULL, updated_at=now(),
                    last_error_code='USER_CANCELLED',
                    last_error_message='Cancelled by user'
                WHERE job.id=%s AND job.telegram_user_id=%s
                  AND job.state NOT IN ('COMPLETED','FAILED','CANCELLED')
                  AND NOT EXISTS (
                      SELECT 1 FROM video_ingestion_batch_items item
                      WHERE item.job_id=job.id
                  )
                RETURNING job.*
                """,
                (job_id, str(user_id)),
            ).fetchone()
        if not row:
            raise ValueError("job cannot be cancelled")
        return dict(row)

    def transition(self, job_id: str, state: JobState, **fields: Any) -> dict[str, Any]:
        allowed = {
            "metadata", "transcript_checksum", "media_sha256", "staging_path",
            "staging_checksum", "selected_destination", "document_id", "last_error_code",
            "last_error_message", "retry_count", "next_attempt_at", "processing_started_at",
            "processing_finished_at", "source_platform", "source_id", "canonical_url",
            "title", "author", "language", "duration_seconds",
        }
        invalid = set(fields) - allowed
        if invalid:
            raise ValueError(f"unsupported fields: {sorted(invalid)}")
        assignments = ["state=%s", "updated_at=now()"]
        values: list[Any] = [state.value]
        for key, value in fields.items():
            assignments.append(f"{key}=%s")
            values.append(Jsonb(value) if key == "metadata" else value)
        values.append(job_id)
        with self.connection() as conn:
            row = conn.execute(
                f"""
                UPDATE video_ingestion_jobs
                SET {', '.join(assignments)}
                WHERE id=%s AND state NOT IN ('PAUSED_USER','CANCELLED')
                RETURNING *
                """,
                values,
            ).fetchone()
            if row and state in {
                JobState.DOWNLOADING,
                JobState.EXTRACTING_SUBTITLES,
                JobState.TRANSCRIBING,
                JobState.BUILDING_MARKDOWN,
                JobState.ENRICHING_METADATA,
                JobState.PERSISTING,
            }:
                batch = conn.execute(
                    """
                    SELECT batch.*
                    FROM video_ingestion_batch_items item
                    JOIN video_ingestion_batches batch
                      ON batch.id=item.batch_id
                    WHERE item.job_id=%s AND item.state='RUNNING'
                    """,
                    (job_id,),
                ).fetchone()
                if batch:
                    self._enqueue_batch_event(
                        conn,
                        self._batch_with_current_work(conn, batch),
                        "PROGRESS",
                    )
            if not row:
                current = conn.execute(
                    "SELECT state FROM video_ingestion_jobs WHERE id=%s",
                    (job_id,),
                ).fetchone()
                if current and current["state"] in {
                    "PAUSED_USER", "CANCELLED"
                }:
                    raise JobControlRequested(current["state"])
        if not row:
            raise KeyError(job_id)
        return dict(row)

    def claim_next(self) -> dict[str, Any] | None:
        with self.connection() as conn:
            overdue_batch_child = conn.execute(
                """
                SELECT 1
                FROM video_ingestion_batch_items item
                JOIN video_ingestion_batches batch ON batch.id=item.batch_id
                JOIN video_ingestion_jobs job ON job.id=item.job_id
                WHERE item.state='QUEUED'
                  AND batch.state IN ('QUEUED','RUNNING')
                  AND job.state IN ('RECEIVED','PERSISTING')
                  AND item.queued_at <= now() - make_interval(secs => %s)
                LIMIT 1
                """,
                (self.settings.batch_child_priority_wait_seconds,),
            ).fetchone()
            # Preserve legacy single-video behavior and priority.
            row = None if overdue_batch_child else conn.execute(
                """
                WITH candidate AS (
                    SELECT job.id FROM video_ingestion_jobs job
                    WHERE job.state IN ('RECEIVED','PERSISTING')
                      AND (next_attempt_at IS NULL OR next_attempt_at <= now())
                      AND NOT EXISTS (
                          SELECT 1 FROM video_ingestion_batch_items item
                          WHERE item.job_id=job.id
                      )
                    ORDER BY job.created_at
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE video_ingestion_jobs j
                SET processing_started_at=coalesce(processing_started_at,now()), updated_at=now()
                FROM candidate WHERE j.id=candidate.id RETURNING j.*
                """
            ).fetchone()
            if row:
                return dict(row)

            # Lock the child first so concurrent workers can claim different items
            # from the same batch. The parent is locked and rechecked immediately
            # afterwards, which makes a concurrent pause/cancel win safely.
            candidate = conn.execute(
                """
                SELECT
                    batch.id AS batch_id,
                    item.id AS item_id,
                    job.id AS job_id
                FROM video_ingestion_batches batch
                JOIN video_ingestion_batch_items item
                  ON item.batch_id=batch.id
                JOIN video_ingestion_jobs job
                  ON job.id=item.job_id
                WHERE batch.state IN ('QUEUED','RUNNING')
                  AND item.state='QUEUED'
                  AND job.state IN ('RECEIVED','PERSISTING')
                  AND (job.next_attempt_at IS NULL OR job.next_attempt_at <= now())
                ORDER BY item.queued_at, item.position
                FOR UPDATE OF item SKIP LOCKED
                LIMIT 1
                """
            ).fetchone()
            if not candidate:
                return None
            locked_job = conn.execute(
                """
                SELECT id FROM video_ingestion_jobs
                WHERE id=%s
                FOR UPDATE
                """,
                (candidate["job_id"],),
            ).fetchone()
            if not locked_job:
                return None
            parent = conn.execute(
                """
                SELECT state FROM video_ingestion_batches
                WHERE id=%s
                FOR UPDATE
                """,
                (candidate["batch_id"],),
            ).fetchone()
            if not parent or parent["state"] not in {"QUEUED", "RUNNING"}:
                return None
            row = conn.execute(
                """
                UPDATE video_ingestion_jobs
                SET processing_started_at=coalesce(processing_started_at,now()),
                    updated_at=now()
                WHERE id=%s
                RETURNING *
                """,
                (candidate["job_id"],),
            ).fetchone()
            conn.execute(
                """
                UPDATE video_ingestion_batch_items
                SET state='RUNNING', started_at=coalesce(started_at,now()),
                    updated_at=now()
                WHERE id=%s
                """,
                (candidate["item_id"],),
            )
            conn.execute(
                """
                UPDATE video_ingestion_batches
                SET state='RUNNING', started_at=coalesce(started_at,now()),
                    updated_at=now()
                WHERE id=%s
                """,
                (candidate["batch_id"],),
            )
            self._recount_batch(conn, candidate["batch_id"])
        return dict(row) if row else None

    def recover_stale(self, stale_after_seconds: int | None = None) -> dict[str, int]:
        stale_after_seconds = self.settings.job_timeout_seconds if stale_after_seconds is None else max(0, stale_after_seconds)
        with self.connection() as conn:
            retried = conn.execute(
                """
                UPDATE video_ingestion_jobs SET state='RECEIVED', retry_count=retry_count+1,
                    next_attempt_at=now(), processing_started_at=NULL, updated_at=now(),
                    last_error_code='WORKER_RESTART'
                WHERE state IN ('DOWNLOADING','EXTRACTING_SUBTITLES','TRANSCRIBING','BUILDING_MARKDOWN','ENRICHING_METADATA')
                  AND updated_at < now() - make_interval(secs => %s)
                  AND retry_count < %s
                RETURNING id
                """,
                (stale_after_seconds, self.settings.max_retries),
            ).fetchall()
            failed = conn.execute(
                """
                UPDATE video_ingestion_jobs SET state='FAILED',updated_at=now(),last_error_code='RETRY_EXHAUSTED'
                WHERE state IN ('DOWNLOADING','EXTRACTING_SUBTITLES','TRANSCRIBING','BUILDING_MARKDOWN','ENRICHING_METADATA')
                  AND updated_at < now() - make_interval(secs => %s)
                  AND retry_count >= %s RETURNING id
                """,
                (stale_after_seconds, self.settings.max_retries),
            ).fetchall()
        return {"retried": len(retried), "failed": len(failed)}

    def select_destination(self, job_id: str, user_id: str, destination: Destination | None) -> str:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT state, telegram_user_id, destination_locked,
                       staging_path
                FROM video_ingestion_jobs WHERE id=%s FOR UPDATE
                """,
                (job_id,),
            ).fetchone()
            if not row:
                return "expired"
            if str(row["telegram_user_id"]) != str(user_id):
                return "forbidden"
            if row["destination_locked"]:
                return "locked"
            if row["state"] in {"COMPLETED", "CANCELLED"}:
                return "finished"
            if row["state"] != "AWAITING_DESTINATION":
                return "not_ready"
            if destination is None:
                conn.execute(
                    "UPDATE video_ingestion_jobs SET state='CANCELLED',updated_at=now(),processing_finished_at=now() WHERE id=%s",
                    (job_id,),
                )
                return "cancelled"
            if destination not in SELECTABLE_DESTINATIONS:
                return "invalid_destination"
            next_state = "PERSISTING" if row["staging_path"] else "RECEIVED"
            conn.execute(
                """
                UPDATE video_ingestion_jobs
                SET state=%s, selected_destination=%s,
                    destination_locked=true, next_attempt_at=now(),
                    updated_at=now()
                WHERE id=%s
                """,
                (next_state, destination.value, job_id),
            )
        return "accepted"

    def find_duplicate(self, *, canonical_url: str | None, platform: str | None, source_id: str | None, media_sha256: str | None, transcript_checksum: str | None) -> dict[str, Any] | None:
        clauses, values = [], []
        for clause, value in (
            ("canonical_url=%s", canonical_url),
            ("(source_platform=%s AND source_id=%s)", (platform, source_id) if platform and source_id else None),
            ("media_sha256=%s", media_sha256),
            ("transcript_checksum=%s", transcript_checksum),
        ):
            if value:
                clauses.append(clause)
                values.extend(value if isinstance(value, tuple) else [value])
        if not clauses:
            return None
        with self.connection() as conn:
            row = conn.execute(
                """SELECT j.*, coalesce(j.staging_path, d.markdown_path) AS reusable_path
                FROM video_ingestion_jobs j
                LEFT JOIN LATERAL (
                    SELECT markdown_path FROM video_knowledge_documents d
                    WHERE d.job_id=j.id AND d.ingestion_status='completed'
                    ORDER BY d.updated_at DESC LIMIT 1
                ) d ON true
                WHERE j.state IN ('AWAITING_DESTINATION','COMPLETED') AND (""" + " OR ".join(clauses) + ") ORDER BY j.created_at LIMIT 1",
                values,
            ).fetchone()
        return dict(row) if row else None

    def upsert_document(self, job: dict[str, Any], markdown_path: Path, checksum: str, status: str) -> str:
        document_id = str(job.get("document_id") or uuid.uuid5(uuid.NAMESPACE_URL, f"video:{job['id']}"))
        metadata = job.get("metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        tags = (metadata.get("enrichment") or {}).get("tags") or []
        original = metadata.get("source") or {}
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO video_knowledge_documents (
                    id, job_id, title, original_title, source_platform, source_url,
                    source_id, author, published_at, captured_at, language, tags,
                    markdown_path, checksum, selected_knowledge_base, ingestion_status,
                    telegram_chat_id, telegram_message_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULLIF(%s,'')::timestamptz,now(),%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id, selected_knowledge_base) DO UPDATE SET ingestion_status=excluded.ingestion_status,
                    markdown_path=excluded.markdown_path, checksum=excluded.checksum, updated_at=now()
                """,
                (document_id, job["id"], job.get("title"), original.get("original_title"),
                 job.get("source_platform"), job.get("canonical_url"), job.get("source_id"),
                 job.get("author"), original.get("published_at") or "", job.get("language"),
                 tags, str(markdown_path), checksum, job.get("selected_destination"), status,
                 job["telegram_chat_id"], job["telegram_message_id"]),
            )
        return document_id
