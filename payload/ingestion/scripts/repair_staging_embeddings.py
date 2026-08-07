#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid
from pathlib import Path

from psycopg.types.json import Jsonb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.ingestion.models import JobState
from backend.ingestion.service import VideoIngestionService


MARKER = "-repair-all-staging-20260727"
DEFER_UNTIL = "2099-01-01T00:00:00Z"


class SilentNotifier:
    def __getattr__(self, _name: str):
        return lambda *args, **kwargs: None


def prepare(service: VideoIngestionService) -> tuple[int, int, int]:
    paths = [str(path) for path in sorted(service.settings.staging_root.glob("*.md"))]
    if not paths:
        return 0, 0, 0

    with service.sql.connection() as conn:
        originals = conn.execute(
            """
            SELECT DISTINCT ON (job.staging_path) job.*
            FROM video_ingestion_jobs job
            WHERE job.staging_path = ANY(%s)
              AND job.selected_destination='thought-politics'
              AND job.author='Cao Cao''s daily observation'
              AND NOT EXISTS (
                  SELECT 1
                  FROM video_knowledge_documents document
                  WHERE document.id=job.document_id
                    AND document.selected_knowledge_base='thought-politics'
                    AND document.ingestion_status='completed'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM video_ingestion_jobs repair
                  WHERE repair.id<>job.id
                    AND repair.source_platform=job.source_platform
                    AND repair.source_id=job.source_id
                    AND repair.selected_destination='thought-politics'
                    AND repair.telegram_message_id LIKE '%%-repair-%%'
                    AND repair.state IN ('PERSISTING','COMPLETED')
              )
            ORDER BY job.staging_path, job.created_at DESC
            """,
            (paths,),
        ).fetchall()

        created = 0
        for original in originals:
            repair_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                (
                    "repair-all-staging:"
                    f"{original['source_platform']}:{original['source_id']}:"
                    "thought-politics"
                ),
            )
            result = conn.execute(
                """
                INSERT INTO video_ingestion_jobs (
                    id, telegram_user_id, telegram_chat_id, telegram_message_id,
                    input_kind, input_value, media_type, caption, forward_origin,
                    state, retry_count, next_attempt_at, processing_started_at,
                    processing_finished_at, source_platform, source_id,
                    canonical_url, title, author, language, duration_seconds,
                    metadata, transcript_checksum, media_sha256, staging_path,
                    staging_checksum, selected_destination, document_id,
                    last_error_code, last_error_message, notification_mode,
                    destination_locked
                )
                VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,
                    'PERSISTING',0,%s::timestamptz,NULL,NULL,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,'thought-politics',%s,NULL,NULL,'INDIVIDUAL',true
                )
                ON CONFLICT (id) DO NOTHING
                RETURNING id
                """,
                (
                    repair_id,
                    original["telegram_user_id"],
                    original["telegram_chat_id"],
                    f"{original['telegram_message_id']}{MARKER}",
                    original["input_kind"],
                    original["input_value"],
                    original["media_type"],
                    original["caption"],
                    Jsonb(original["forward_origin"]),
                    DEFER_UNTIL,
                    original["source_platform"],
                    original["source_id"],
                    original["canonical_url"],
                    original["title"],
                    original["author"],
                    original["language"],
                    original["duration_seconds"],
                    Jsonb(original["metadata"]),
                    original["transcript_checksum"],
                    original["media_sha256"],
                    original["staging_path"],
                    original["staging_checksum"],
                    original["document_id"],
                ),
            ).fetchone()
            created += int(result is not None)

        queued = conn.execute(
            """
            SELECT count(*) AS count
            FROM video_ingestion_jobs
            WHERE telegram_message_id LIKE %s
              AND state IN ('PERSISTING','FAILED')
            """,
            (f"%{MARKER}",),
        ).fetchone()["count"]

    return len(paths), created, queued


def process(service: VideoIngestionService) -> tuple[int, int]:
    completed = 0
    failed = 0
    while True:
        with service.sql.connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM video_ingestion_jobs
                WHERE telegram_message_id LIKE %s
                  AND state IN ('PERSISTING','FAILED')
                ORDER BY created_at, title
                LIMIT 1
                """,
                (f"%{MARKER}",),
            ).fetchone()
        if row is None:
            break

        job = dict(row)
        job_id = str(job["id"])
        service.sql.transition(
            job_id,
            JobState.PERSISTING,
            next_attempt_at=DEFER_UNTIL,
            processing_started_at=job.get("processing_started_at"),
            processing_finished_at=None,
            last_error_code=None,
            last_error_message=None,
        )
        try:
            service._persist(service.sql.get_job(job_id))
            completed += 1
            logging.info("completed source_id=%s title=%s", job["source_id"], job["title"])
        except Exception as exc:  # noqa: BLE001
            failed += 1
            service.sql.transition(
                job_id,
                JobState.FAILED,
                last_error_code="STAGING_REPAIR_FAILED",
                last_error_message=str(exc)[:1000],
                processing_finished_at=None,
                next_attempt_at=DEFER_UNTIL,
            )
            logging.exception("failed source_id=%s title=%s", job["source_id"], job["title"])
    return completed, failed


def wait_for_preexisting_repairs(service: VideoIngestionService) -> None:
    while True:
        with service.sql.connection() as conn:
            active = conn.execute(
                """
                SELECT count(*) AS count
                FROM video_ingestion_jobs
                WHERE telegram_message_id LIKE '%%-repair-20260727'
                  AND state='PERSISTING'
                """
            ).fetchone()["count"]
        if active == 0:
            return
        logging.info("waiting_for_preexisting_repairs active=%s", active)
        time.sleep(15)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    service = VideoIngestionService(notifier=SilentNotifier())
    files, created, queued = prepare(service)
    logging.info("staging_files=%s created=%s queued=%s", files, created, queued)
    if args.prepare_only:
        return 0
    wait_for_preexisting_repairs(service)
    completed, failed = process(service)
    logging.info("repair_finished completed=%s failed=%s", completed, failed)
    return int(failed > 0)


if __name__ == "__main__":
    raise SystemExit(main())
