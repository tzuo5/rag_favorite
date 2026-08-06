from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from dotenv import dotenv_values
from psycopg import sql
from psycopg.rows import dict_row

from backend.ingestion.config import Settings


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.skipif(
        os.getenv("RUN_AUTHOR_BATCH_MIGRATION_TESTS") != "1",
        reason="set RUN_AUTHOR_BATCH_MIGRATION_TESTS=1 for isolated PostgreSQL migration tests",
    ),
]

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
TABLES_0003 = {
    "video_author_discoveries",
    "video_author_discovery_items",
    "video_ingestion_batches",
    "video_ingestion_batch_items",
    "video_platform_request_gates",
    "video_ingestion_batch_notifications",
}


def database_connection() -> psycopg.Connection:
    values = dotenv_values(Settings().database_env)
    return psycopg.connect(
        host="127.0.0.1",
        port=5432,
        dbname=values["POSTGRES_DB"],
        user=values["POSTGRES_USER"],
        password=values["POSTGRES_PASSWORD"],
        connect_timeout=10,
        autocommit=True,
        row_factory=dict_row,
    )


def run_script(connection: psycopg.Connection, filename: str) -> None:
    connection.execute((MIGRATIONS / filename).read_text(encoding="utf-8"))


@pytest.fixture
def isolated_schema():
    schema = f"test_author_batch_{uuid.uuid4().hex}"
    assert schema.startswith("test_author_batch_")
    with database_connection() as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection.execute(
            sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema))
        )
        try:
            yield connection
        finally:
            connection.rollback()
            connection.execute("SET search_path TO public")
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def installed_tables(connection: psycopg.Connection) -> set[str]:
    rows = connection.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = current_schema()
        """
    ).fetchall()
    return {row["table_name"] for row in rows}


def install_0001_and_0002(connection: psycopg.Connection) -> None:
    run_script(connection, "0001_video_ingestion.sql")
    run_script(connection, "0002_multi_destination_documents.sql")


def insert_legacy_job(connection: psycopg.Connection) -> uuid.UUID:
    job_id = uuid.uuid4()
    connection.execute(
        """
        INSERT INTO video_ingestion_jobs (
            id, telegram_user_id, telegram_chat_id, telegram_message_id,
            input_kind, input_value, state
        ) VALUES (%s, 'test-user', 'test-chat', 'test-message', 'url',
                  'https://youtu.be/test', 'RECEIVED')
        """,
        (job_id,),
    )
    return job_id


def test_fresh_0001_to_0003_install(isolated_schema) -> None:
    connection = isolated_schema
    install_0001_and_0002(connection)
    run_script(connection, "0003_author_batch_ingestion.sql")
    run_script(connection, "0003_author_batch_ingestion.sql")

    assert TABLES_0003 <= installed_tables(connection)
    columns = connection.execute(
        """
        SELECT column_name, column_default, is_nullable
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'video_ingestion_jobs'
          AND column_name IN ('notification_mode', 'destination_locked')
        """
    ).fetchall()
    assert {row["column_name"] for row in columns} == {
        "notification_mode", "destination_locked",
    }
    assert all(row["is_nullable"] == "NO" for row in columns)
    index_names = {
        row["indexname"]
        for row in connection.execute(
            """
            SELECT indexname FROM pg_indexes
            WHERE schemaname = current_schema()
            """
        ).fetchall()
    }
    assert {
        "video_author_discoveries_active_key",
        "video_author_discovery_items_selection_idx",
        "video_ingestion_batches_active_user_key",
        "video_ingestion_batch_items_state_position_idx",
        "video_platform_request_gates_schedule_idx",
        "video_ingestion_batch_notifications_pending_idx",
    } <= index_names
    foreign_keys = connection.execute(
        """
        SELECT count(*) AS count
        FROM pg_constraint constraint_record
        JOIN pg_class table_record ON table_record.oid = constraint_record.conrelid
        WHERE constraint_record.connamespace = current_schema()::regnamespace
          AND constraint_record.contype = 'f'
          AND table_record.relname = ANY(%s)
        """,
        (list(TABLES_0003),),
    ).fetchone()["count"]
    assert foreign_keys == 6


def test_existing_jobs_upgrade_and_empty_rollback(isolated_schema) -> None:
    connection = isolated_schema
    install_0001_and_0002(connection)
    job_id = insert_legacy_job(connection)
    run_script(connection, "0003_author_batch_ingestion.sql")

    row = connection.execute(
        """
        SELECT notification_mode, destination_locked
        FROM video_ingestion_jobs WHERE id = %s
        """,
        (job_id,),
    ).fetchone()
    assert row == {
        "notification_mode": "INDIVIDUAL",
        "destination_locked": False,
    }

    run_script(connection, "rollback/0003_author_batch_ingestion.sql")
    assert not (TABLES_0003 & installed_tables(connection))
    assert connection.execute(
        "SELECT state FROM video_ingestion_jobs WHERE id = %s", (job_id,)
    ).fetchone()["state"] == "RECEIVED"
    remaining_columns = connection.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'video_ingestion_jobs'
          AND column_name IN ('notification_mode', 'destination_locked')
        """
    ).fetchall()
    assert remaining_columns == []


def test_rollback_refuses_to_delete_batch_audit_data(isolated_schema) -> None:
    connection = isolated_schema
    install_0001_and_0002(connection)
    run_script(connection, "0003_author_batch_ingestion.sql")
    discovery_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    connection.execute(
        """
        INSERT INTO video_author_discoveries (
            id, telegram_user_id, telegram_chat_id, telegram_message_id,
            input_url, platform, author_id, canonical_author_url, author_name,
            state, scan_limit, expires_at, finished_at
        ) VALUES (
            %s, 'test-user', 'test-chat', 'test-message',
            'https://youtube.com/@example', 'youtube', 'UC_TEST',
            'https://youtube.com/@example/videos', 'Example',
            'CONSUMED', 50, now() + interval '30 minutes', now()
        )
        """,
        (discovery_id,),
    )
    connection.execute(
        """
        INSERT INTO video_ingestion_batches (
            id, discovery_id, telegram_user_id, telegram_chat_id,
            telegram_message_id, platform, author_id, author_url, author_name,
            selection_policy, selected_destination, state, total_count
        ) VALUES (
            %s, %s, 'test-user', 'test-chat', 'test-message', 'youtube',
            'UC_TEST', 'https://youtube.com/@example/videos', 'Example',
            '{"version": 1, "kind": "latest", "limit": 10}'::jsonb,
            'main', 'QUEUED', 1
        )
        """,
        (batch_id, discovery_id),
    )

    with pytest.raises(psycopg.errors.RaiseException):
        run_script(connection, "rollback/0003_author_batch_ingestion.sql")
    connection.rollback()
    assert "video_ingestion_batches" in installed_tables(connection)


def test_0004_upgrades_legacy_platform_and_batch_limit_constraints(
    isolated_schema,
) -> None:
    connection = isolated_schema
    install_0001_and_0002(connection)
    run_script(connection, "0003_author_batch_ingestion.sql")
    # Build the legacy constraint shape, then verify the forward migration is
    # repeatable for an already deployed database.
    run_script(connection, "rollback/0004_multiplatform_author_batch.sql")
    with pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(
            """
            INSERT INTO video_author_discoveries (
                id, telegram_user_id, telegram_chat_id, telegram_message_id,
                input_url, platform, canonical_author_url, state, scan_limit,
                expires_at
            ) VALUES (
                %s, 'bili-user', 'bili-chat', 'bili-message',
                'https://space.bilibili.com/123/video', 'bilibili',
                'https://space.bilibili.com/123/video', 'QUEUED', 10,
                now() + interval '30 minutes'
            )
            """,
            (uuid.uuid4(),),
        )
    connection.rollback()

    run_script(connection, "0004_multiplatform_author_batch.sql")
    run_script(connection, "0004_multiplatform_author_batch.sql")
    discovery_id = uuid.uuid4()
    connection.execute(
        """
        INSERT INTO video_author_discoveries (
            id, telegram_user_id, telegram_chat_id, telegram_message_id,
            input_url, platform, canonical_author_url, state, scan_limit,
            expires_at
        ) VALUES (
            %s, 'bili-user', 'bili-chat', 'bili-message',
            'https://space.bilibili.com/123/video', 'bilibili',
            'https://space.bilibili.com/123/video', 'QUEUED', 10,
            now() + interval '30 minutes'
        )
        """,
        (discovery_id,),
    )
    definition = connection.execute(
        """
        SELECT pg_get_constraintdef(oid) AS definition
        FROM pg_constraint
        WHERE conrelid='video_ingestion_batches'::regclass
          AND conname='video_ingestion_batches_total_count_check'
        """
    ).fetchone()["definition"]
    assert "50" in definition

    with pytest.raises(psycopg.errors.RaiseException):
        run_script(connection, "rollback/0004_multiplatform_author_batch.sql")
    connection.rollback()


def test_0005_migrates_legacy_main_document_by_markdown_path(
    isolated_schema,
) -> None:
    connection = isolated_schema
    install_0001_and_0002(connection)
    job_id = insert_legacy_job(connection)
    document_id = uuid.uuid4()
    connection.execute(
        """
        UPDATE video_ingestion_jobs
        SET state='COMPLETED', selected_destination='main'
        WHERE id=%s
        """,
        (job_id,),
    )
    connection.execute(
        """
        INSERT INTO video_knowledge_documents (
            id, job_id, title, source_platform, captured_at, markdown_path,
            checksum, selected_knowledge_base, ingestion_status,
            telegram_chat_id, telegram_message_id
        ) VALUES (
            %s, %s, 'Legacy technology video', 'youtube', now(),
            '/home/ubuntu/知识库/Technology/3. Video Transcripts/legacy.md',
            'checksum', 'main', 'completed', 'test-chat', 'test-message'
        )
        """,
        (document_id, job_id),
    )
    run_script(connection, "0003_author_batch_ingestion.sql")
    run_script(connection, "0004_multiplatform_author_batch.sql")
    run_script(connection, "0005_explicit_knowledge_destinations.sql")
    run_script(connection, "0005_explicit_knowledge_destinations.sql")

    job = connection.execute(
        """
        SELECT selected_destination
        FROM video_ingestion_jobs WHERE id=%s
        """,
        (job_id,),
    ).fetchone()
    document = connection.execute(
        """
        SELECT selected_knowledge_base
        FROM video_knowledge_documents WHERE id=%s
        """,
        (document_id,),
    ).fetchone()
    assert job["selected_destination"] == "tech"
    assert document["selected_knowledge_base"] == "tech"
    columns = connection.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema=current_schema()
          AND table_name='video_author_discoveries'
          AND column_name='existing_destination_counts'
        """
    ).fetchall()
    assert columns == [{"column_name": "existing_destination_counts"}]

    with pytest.raises(psycopg.errors.RaiseException):
        run_script(
            connection,
            "rollback/0005_explicit_knowledge_destinations.sql",
        )
    connection.rollback()


def test_0006_removes_fifty_item_constraints_and_is_idempotent(
    isolated_schema,
) -> None:
    connection = isolated_schema
    install_0001_and_0002(connection)
    run_script(connection, "0003_author_batch_ingestion.sql")
    run_script(connection, "0006_unlimited_author_batches.sql")
    run_script(connection, "0006_unlimited_author_batches.sql")

    definitions = {
        row["constraint_name"]: row["definition"]
        for row in connection.execute(
            """
            SELECT constraint_record.conname AS constraint_name,
                   pg_get_constraintdef(constraint_record.oid) AS definition
            FROM pg_constraint constraint_record
            JOIN pg_class table_record
              ON table_record.oid=constraint_record.conrelid
            WHERE table_record.relname IN (
                'video_author_discoveries', 'video_ingestion_batches'
            )
              AND constraint_record.conname IN (
                'video_author_discoveries_scan_limit_check',
                'video_ingestion_batches_total_count_check'
              )
            """
        ).fetchall()
    }
    assert "scan_limit >= 0" in definitions[
        "video_author_discoveries_scan_limit_check"
    ]
    assert "total_count >= 1" in definitions[
        "video_ingestion_batches_total_count_check"
    ]
    assert "50" not in definitions[
        "video_ingestion_batches_total_count_check"
    ]
