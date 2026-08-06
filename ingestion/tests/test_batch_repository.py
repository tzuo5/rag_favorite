from __future__ import annotations

import hashlib
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from dotenv import dotenv_values
from psycopg import sql
from psycopg.rows import dict_row

from backend.ingestion.batch_repository import (
    ActiveBatchExistsError,
    BatchBudgetExceededError,
    BatchStateError,
)
from backend.ingestion.config import Settings
from backend.ingestion.models import BatchState, Destination, JobState
from backend.ingestion.repository import JobControlRequested, SqlRepository
from backend.ingestion.service import VideoIngestionService

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.skipif(
        os.getenv("RUN_AUTHOR_BATCH_REPOSITORY_TESTS") != "1",
        reason="set RUN_AUTHOR_BATCH_REPOSITORY_TESTS=1 for isolated repository tests",
    ),
]

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"


def database_parameters() -> dict[str, object]:
    values = dotenv_values(Settings().database_env)
    return {
        "host": "127.0.0.1",
        "port": 5432,
        "dbname": values["POSTGRES_DB"],
        "user": values["POSTGRES_USER"],
        "password": values["POSTGRES_PASSWORD"],
        "connect_timeout": 10,
        "row_factory": dict_row,
    }


@pytest.fixture
def repository_schema():
    schema = f"test_batch_repository_{uuid.uuid4().hex}"
    parameters = database_parameters()
    with psycopg.connect(**parameters, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        admin.execute(
            sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema))
        )
        for migration in (
            "0001_video_ingestion.sql",
            "0002_multi_destination_documents.sql",
            "0003_author_batch_ingestion.sql",
            "0006_unlimited_author_batches.sql",
            "0007_work_controls.sql",
        ):
            admin.execute((MIGRATIONS / migration).read_text(encoding="utf-8"))

    def connection_factory() -> psycopg.Connection:
        return psycopg.connect(
            **parameters,
            options=f"-csearch_path={schema},public",
        )

    repository = SqlRepository(Settings(), connection_factory=connection_factory)
    try:
        yield repository, connection_factory
    finally:
        with psycopg.connect(**parameters, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def seed_discovery(
    connection_factory,
    *,
    user_id: str = "batch-user",
    works: int = 2,
) -> uuid.UUID:
    discovery_id = uuid.uuid4()
    with connection_factory() as connection:
        connection.execute(
            """
            INSERT INTO video_author_discoveries (
                id, telegram_user_id, telegram_chat_id, telegram_message_id,
                input_url, platform, author_id, canonical_author_url,
                author_name, state, scan_limit, discovered_count,
                eligible_count, expires_at
            ) VALUES (
                %s,%s,'batch-chat','batch-message',
                'https://youtube.com/@batch-test','youtube','UC_BATCH_TEST',
                'https://youtube.com/@batch-test/videos','Batch Test',
                'READY',50,%s,%s,now() + interval '30 minutes'
            )
            """,
            (discovery_id, user_id, works, works),
        )
        for position in range(1, works + 1):
            connection.execute(
                """
                INSERT INTO video_author_discovery_items (
                    discovery_id, platform, source_id, canonical_url, title,
                    duration_seconds, content_type, position, eligibility
                ) VALUES (
                    %s,'youtube',%s,%s,%s,60,'VIDEO',%s,'ELIGIBLE'
                )
                """,
                (
                    discovery_id,
                    f"video-{discovery_id.hex[:8]}-{position}",
                    f"https://www.youtube.com/watch?v={discovery_id.hex[:8]}{position}",
                    f"Video {position}",
                    position,
                ),
            )
    return discovery_id


def seed_completed_document(
    connection_factory,
    *,
    source_id: str,
    canonical_url: str,
    destination: str,
) -> int:
    job_id = uuid.uuid4()
    document_id = uuid.uuid4()
    with connection_factory() as connection:
        connection.execute(
            """
            INSERT INTO video_ingestion_jobs (
                id, telegram_user_id, telegram_chat_id, telegram_message_id,
                input_kind, input_value, state, source_platform, source_id,
                canonical_url, selected_destination
            ) VALUES (
                %s,'document-user','document-chat','document-message',
                'url',%s,'COMPLETED','youtube',%s,%s,%s
            )
            """,
            (job_id, canonical_url, source_id, canonical_url, destination),
        )
        return connection.execute(
            """
            INSERT INTO video_knowledge_documents (
                id, job_id, title, source_platform, source_url, source_id,
                captured_at, markdown_path, checksum, selected_knowledge_base,
                ingestion_status, telegram_chat_id, telegram_message_id
            ) VALUES (
                %s,%s,'Existing','youtube',%s,%s,now(),'/tmp/existing.md',
                'checksum',%s,'completed','document-chat','document-message'
            )
            RETURNING record_id
            """,
            (document_id, job_id, canonical_url, source_id, destination),
        ).fetchone()["record_id"]


def get_batch_facts(connection_factory, batch_id) -> dict[str, object]:
    with connection_factory() as connection:
        batch = connection.execute(
            "SELECT * FROM video_ingestion_batches WHERE id=%s", (batch_id,)
        ).fetchone()
        items = connection.execute(
            """
            SELECT * FROM video_ingestion_batch_items
            WHERE batch_id=%s ORDER BY position
            """,
            (batch_id,),
        ).fetchall()
        jobs = connection.execute(
            """
            SELECT job.* FROM video_ingestion_jobs job
            JOIN video_ingestion_batch_items item ON item.job_id=job.id
            WHERE item.batch_id=%s ORDER BY item.position
            """,
            (batch_id,),
        ).fetchall()
    return {"batch": batch, "items": items, "jobs": jobs}


def test_confirm_is_atomic_idempotent_and_destination_aware(
    repository_schema,
) -> None:
    repository, connection_factory = repository_schema
    discovery_id = seed_discovery(connection_factory)
    with connection_factory() as connection:
        works = connection.execute(
            """
            SELECT source_id, canonical_url
            FROM video_author_discovery_items
            WHERE discovery_id=%s ORDER BY position
            """,
            (discovery_id,),
        ).fetchall()
    existing_record_id = seed_completed_document(
        connection_factory,
        source_id=works[0]["source_id"],
        canonical_url=works[0]["canonical_url"],
        destination="main",
    )

    created = repository.confirm_discovery(
        discovery_id, "batch-user", Destination.MAIN
    )
    replay = repository.confirm_discovery(
        discovery_id, "batch-user", Destination.COOKING
    )
    assert replay["id"] == created["id"]
    assert replay["selected_destination"] == "main"

    facts = get_batch_facts(connection_factory, created["id"])
    assert facts["batch"]["state"] == "QUEUED"
    assert facts["batch"]["queued_count"] == 1
    assert facts["batch"]["skipped_existing_count"] == 1
    assert [item["state"] for item in facts["items"]] == [
        "SKIPPED_EXISTING",
        "QUEUED",
    ]
    assert facts["items"][0]["existing_document_record_id"] == existing_record_id
    assert len(facts["jobs"]) == 1
    assert facts["jobs"][0]["notification_mode"] == "BATCH_SILENT"
    assert facts["jobs"][0]["destination_locked"] is True
    assert facts["jobs"][0]["selected_destination"] == "main"


def test_concurrent_confirmation_has_one_winner_per_discovery_and_user(
    repository_schema,
) -> None:
    repository, connection_factory = repository_schema
    discovery_id = seed_discovery(connection_factory, works=1)
    barrier = threading.Barrier(2)

    def confirm_same():
        barrier.wait()
        return repository.confirm_discovery(
            discovery_id, "batch-user", Destination.MAIN
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: confirm_same(), range(2)))
    assert results[0]["id"] == results[1]["id"]
    with connection_factory() as connection:
        assert connection.execute(
            "SELECT count(*) AS count FROM video_ingestion_batches"
        ).fetchone()["count"] == 1
        assert connection.execute(
            "SELECT count(*) AS count FROM video_ingestion_batch_items"
        ).fetchone()["count"] == 1
        assert connection.execute(
            """
            SELECT count(*) AS count FROM video_ingestion_jobs
            WHERE notification_mode='BATCH_SILENT'
            """
        ).fetchone()["count"] == 1

    other_discovery = seed_discovery(
        connection_factory, user_id="batch-user", works=1
    )
    with pytest.raises(ActiveBatchExistsError):
        repository.confirm_discovery(
            other_discovery, "batch-user", Destination.MAIN
        )
    with connection_factory() as connection:
        assert connection.execute(
            """
            SELECT state FROM video_author_discoveries WHERE id=%s
            """,
            (other_discovery,),
        ).fetchone()["state"] == "READY"


def test_parallel_claims_are_distinct_and_pause_blocks_new_claims(
    repository_schema,
) -> None:
    repository, connection_factory = repository_schema
    discovery_id = seed_discovery(connection_factory, works=3)
    batch = repository.confirm_discovery(
        discovery_id, "batch-user", Destination.MAIN
    )
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait()
        return repository.claim_next()

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(executor.map(lambda _: claim(), range(2)))
    assert all(claimed)
    assert len({row["id"] for row in claimed}) == 2
    facts = get_batch_facts(connection_factory, batch["id"])
    assert facts["batch"]["running_count"] == 2
    assert facts["batch"]["queued_count"] == 1
    status = repository.get_owned_batch(batch["id"], "batch-user")
    assert status["current_title"] in {"Video 1", "Video 2"}
    assert status["current_job_state"] == "RECEIVED"
    assert status["started_count"] == 2

    paused = repository.pause_batch(batch["id"], "batch-user")
    assert paused["state"] == "PAUSED_USER"
    assert repository.claim_next() is None
    resumed = repository.resume_batch(batch["id"], "batch-user")
    assert resumed["state"] == "RUNNING"
    third = repository.claim_next()
    assert third and third["id"] not in {row["id"] for row in claimed}


def test_child_transitions_recount_and_cancel_converges(
    repository_schema,
) -> None:
    repository, connection_factory = repository_schema
    discovery_id = seed_discovery(connection_factory, works=3)
    batch = repository.confirm_discovery(
        discovery_id, "batch-user", Destination.MAIN
    )
    first = repository.claim_next()
    second = repository.claim_next()
    assert first and second

    retried = repository.retry_batch_child(
        first["id"], "TEMPORARY", "try again", first["created_at"]
    )
    assert retried["item"]["state"] == "QUEUED"
    assert retried["job"]["retry_count"] == 1
    first_again = repository.claim_next()
    assert first_again and first_again["id"] == first["id"]
    deferred = repository.retry_batch_child(
        first["id"],
        "REQUEST_INTERVAL",
        "wait for platform pacing",
        first["created_at"],
        increment_retry=False,
    )
    assert deferred["item"]["state"] == "QUEUED"
    assert deferred["job"]["retry_count"] == 1
    first_again = repository.claim_next()
    assert first_again and first_again["id"] == first["id"]
    completed = repository.complete_batch_child(first["id"], uuid.uuid4())
    assert completed["item"]["state"] == "COMPLETED"
    # A duplicate terminal event is a no-op and cannot increment cached counts.
    duplicate = repository.complete_batch_child(first["id"], uuid.uuid4())
    assert duplicate["item"]["state"] == "COMPLETED"

    cancelling = repository.request_batch_cancel(batch["id"], "batch-user")
    assert cancelling["state"] == "CANCELLING"
    assert cancelling["completed_count"] == 1
    assert cancelling["running_count"] == 1
    assert cancelling["cancelled_count"] == 1
    terminal = repository.cancel_batch_child(second["id"])
    assert terminal["batch"]["state"] == "CANCELLED"
    assert terminal["batch"]["completed_count"] == 1
    assert terminal["batch"]["cancelled_count"] == 2
    assert terminal["batch"]["finished_at"] is not None
    with connection_factory() as connection:
        events = connection.execute(
            """
            SELECT event_type, idempotency_key
            FROM video_ingestion_batch_notifications
            WHERE batch_id=%s ORDER BY created_at
            """,
            (batch["id"],),
        ).fetchall()
    assert events[0]["event_type"] == "CREATED"
    assert events[-1]["event_type"] == "TERMINAL"
    assert len({row["idempotency_key"] for row in events}) == len(events)


def test_staged_running_child_is_requeued_for_persistence(
    repository_schema,
) -> None:
    repository, connection_factory = repository_schema
    discovery_id = seed_discovery(connection_factory, works=1)
    batch = repository.confirm_discovery(
        discovery_id, "batch-user", Destination.MAIN
    )
    child = repository.claim_next()
    assert child

    repository.transition(
        str(child["id"]),
        JobState.PERSISTING,
        next_attempt_at=child["created_at"],
    )
    queued = repository.queue_batch_child_persistence(child["id"])
    assert queued["job"]["state"] == "PERSISTING"
    assert queued["item"]["state"] == "QUEUED"
    assert queued["batch"]["queued_count"] == 1
    assert queued["batch"]["running_count"] == 0
    status = repository.get_owned_batch(batch["id"], "batch-user")
    assert status["started_count"] == 1

    persistence = repository.claim_next()
    assert persistence and persistence["id"] == child["id"]
    facts = get_batch_facts(connection_factory, batch["id"])
    assert facts["items"][0]["state"] == "RUNNING"
    assert facts["jobs"][0]["state"] == "PERSISTING"


def test_individual_job_controls_are_atomic_and_exclude_batch_children(
    repository_schema,
) -> None:
    repository, _ = repository_schema
    job = repository.create_job(
        user_id="single-user",
        chat_id="single-chat",
        message_id="single-message",
        input_kind="url",
        input_value="https://www.youtube.com/watch?v=single-control",
    )
    paused = repository.pause_job(job["id"], "single-user")
    assert paused["state"] == "PAUSED_USER"
    assert repository.claim_next() is None

    resumed = repository.resume_job(job["id"], "single-user")
    assert resumed["state"] == "RECEIVED"
    assert repository.claim_next()["id"] == job["id"]

    cancelled = repository.cancel_job(job["id"], "single-user")
    assert cancelled["state"] == "CANCELLED"
    with pytest.raises(JobControlRequested) as stopped:
        repository.transition(job["id"], JobState.TRANSCRIBING)
    assert stopped.value.state == "CANCELLED"


def test_execution_race_dedup_and_platform_pause_are_atomic(
    repository_schema,
) -> None:
    repository, connection_factory = repository_schema
    discovery_id = seed_discovery(connection_factory, works=2)
    batch = repository.confirm_discovery(
        discovery_id, "batch-user", Destination.MAIN
    )
    first = repository.claim_next()
    assert first
    record_id = seed_completed_document(
        connection_factory,
        source_id=first["source_id"],
        canonical_url=first["canonical_url"],
        destination="main",
    )
    skipped = repository.skip_batch_child_existing(first["id"], record_id)
    assert skipped["item"]["state"] == "SKIPPED_EXISTING"
    assert skipped["item"]["job_id"] is None
    assert skipped["batch"]["skipped_existing_count"] == 1

    second = repository.claim_next()
    assert second
    paused = repository.pause_and_requeue_batch_child(
        second["id"],
        state=BatchState.PAUSED_AUTH,
        error_code="AUTH_EXPIRED",
        error_message="Authentication expired",
        next_attempt_at=second["created_at"],
    )
    assert paused["batch"]["state"] == "PAUSED_AUTH"
    assert paused["item"]["state"] == "QUEUED"
    assert paused["job"]["state"] == "RECEIVED"
    assert repository.claim_next() is None


def test_verified_xiaohongshu_recovery_resumes_only_auth_paused_batches(
    repository_schema,
) -> None:
    repository, connection_factory = repository_schema
    discovery_id = seed_discovery(connection_factory, works=1)
    with connection_factory() as connection:
        connection.execute(
            """
            UPDATE video_author_discoveries
            SET platform='xiaohongshu',
                input_url='https://www.xiaohongshu.com/user/profile/test',
                canonical_author_url=
                    'https://www.xiaohongshu.com/user/profile/test',
                author_id='xhs-author'
            WHERE id=%s
            """,
            (discovery_id,),
        )
        connection.execute(
            """
            UPDATE video_author_discovery_items
            SET platform='xiaohongshu',
                canonical_url='https://www.xiaohongshu.com/explore/test'
            WHERE discovery_id=%s
            """,
            (discovery_id,),
        )
    batch = repository.confirm_discovery(
        discovery_id, "batch-user", Destination.MAIN
    )
    child = repository.claim_next()
    assert child
    repository.pause_and_requeue_batch_child(
        child["id"],
        state=BatchState.PAUSED_AUTH,
        error_code="AUTH_EXPIRED",
        error_message="Authentication expired",
        next_attempt_at=child["created_at"],
    )

    assert repository.resume_recovered_xiaohongshu_batches() == 1
    facts = get_batch_facts(connection_factory, batch["id"])
    assert facts["batch"]["state"] == "RUNNING"
    assert facts["batch"]["pause_code"] is None
    assert facts["items"][0]["state"] == "QUEUED"
    assert facts["jobs"][0]["state"] == "RECEIVED"
    with connection_factory() as connection:
        gate = connection.execute(
            """
            SELECT circuit_state, last_error_code
            FROM video_platform_request_gates
            WHERE platform='xiaohongshu'
            """
        ).fetchone()
        resumed = connection.execute(
            """
            SELECT count(*) AS count
            FROM video_ingestion_batch_notifications
            WHERE batch_id=%s AND event_type='RESUMED'
            """,
            (batch["id"],),
        ).fetchone()["count"]
    assert gate == {"circuit_state": "CLOSED", "last_error_code": None}
    assert resumed == 1


def test_accurate_duration_can_fail_only_the_over_budget_child(
    repository_schema,
) -> None:
    repository, connection_factory = repository_schema
    discovery_id = seed_discovery(connection_factory, works=2)
    with connection_factory() as connection:
        connection.execute(
            """
            UPDATE video_author_discovery_items SET duration_seconds=NULL
            WHERE discovery_id=%s
            """,
            (discovery_id,),
        )
    settings = Settings(
        enforce_batch_estimated_duration_budget=True,
        max_batch_estimated_duration_seconds=7200,
        max_batch_unknown_duration_reservation_seconds=3600,
    )
    limited = SqlRepository(settings, connection_factory=connection_factory)
    batch = limited.confirm_discovery(
        discovery_id, "batch-user", Destination.MAIN
    )
    first = limited.claim_next()
    assert first
    assert limited.record_batch_child_duration(first["id"], 3000)
    limited.complete_batch_child(first["id"], uuid.uuid4())
    second = limited.claim_next()
    assert second
    assert not limited.record_batch_child_duration(second["id"], 5000)
    facts = get_batch_facts(connection_factory, batch["id"])
    assert facts["batch"]["state"] == "COMPLETED_WITH_ERRORS"
    assert facts["batch"]["completed_count"] == 1
    assert facts["batch"]["failed_count"] == 1


def test_outbox_delivery_metrics_and_read_only_consistency_audit(
    repository_schema,
) -> None:
    repository, connection_factory = repository_schema
    discovery_id = seed_discovery(connection_factory, works=1)
    batch = repository.confirm_discovery(
        discovery_id, "batch-user", Destination.MAIN
    )
    metrics = repository.batch_metrics()
    assert metrics["outbox_pending"] == 1
    assert metrics["consistency_issue_count"] == 0
    notification = repository.claim_batch_notification()
    assert notification["event_type"] == "CREATED"
    assert notification["attempts"] == 1
    repository.mark_batch_notification_delivered(notification["id"], 4242)
    assert repository.latest_batch_notification_message_id(batch["id"]) == 4242
    assert repository.batch_metrics()["outbox_pending"] == 0
    assert repository.audit_batch_consistency() == []

    with connection_factory() as connection:
        connection.execute(
            """
            UPDATE video_ingestion_batches
            SET queued_count=0 WHERE id=%s
            """,
            (batch["id"],),
        )
    issues = repository.audit_batch_consistency()
    assert issues == [{"batch_id": batch["id"], "issue": "COUNT_DRIFT"}]
    # The report-only audit must not repair the row.
    assert repository.audit_batch_consistency() == issues


def test_isolated_ten_item_persisting_e2e(
    repository_schema,
    tmp_path,
) -> None:
    repository, connection_factory = repository_schema
    discovery_id = seed_discovery(connection_factory, works=10)
    batch = repository.confirm_discovery(
        discovery_id, "batch-user", Destination.MAIN
    )

    class FakeVectors:
        def __init__(self):
            self.paths = []

        def ingest(self, destination, path):
            self.paths.append(path)

    class FakeDestinations:
        def __init__(self):
            self.vectors = FakeVectors()

        def target_path(
            self,
            destination,
            title,
            document_id,
            source_markdown=None,
            metadata=None,
        ):
            assert source_markdown is not None
            assert metadata is not None
            return tmp_path / "knowledge" / f"{document_id}.md"

    class NoNotifications:
        def __getattr__(self, name):
            return lambda *args: (_ for _ in ()).throw(
                AssertionError(f"unexpected individual notification: {name}")
            )

    service = object.__new__(VideoIngestionService)
    service.settings = Settings(
        temp_root=tmp_path / "jobs",
        staging_root=tmp_path / "staging",
    )
    service.sql = repository
    service.destinations = FakeDestinations()
    service.notifier = NoNotifications()
    service.settings.ensure_directories()

    facts = get_batch_facts(connection_factory, batch["id"])
    for index, job in enumerate(facts["jobs"], start=1):
        document_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"isolated-batch-e2e:{job['id']}"
        )
        staging = service.settings.staging_root / f"{job['id']}.md"
        content = f"# Isolated batch item {index}\n\nSynthetic transcript.\n"
        staging.write_text(content, encoding="utf-8")
        checksum = hashlib.sha256(staging.read_bytes()).hexdigest()
        repository.transition(
            str(job["id"]),
            JobState.PERSISTING,
            staging_path=str(staging),
            staging_checksum=checksum,
            document_id=str(document_id),
            title=f"Isolated batch item {index}",
            metadata={
                "source": {"original_title": f"Fixture {index}"},
                "enrichment": {"tags": ["isolated", "batch"]},
            },
            next_attempt_at=job["created_at"],
        )

    for _ in range(10):
        child = repository.claim_next()
        assert child and child["notification_mode"] == "BATCH_SILENT"
        service._persist(child)
    assert repository.claim_next() is None

    facts = get_batch_facts(connection_factory, batch["id"])
    assert facts["batch"]["state"] == "COMPLETED"
    assert facts["batch"]["completed_count"] == 10
    assert len(service.destinations.vectors.paths) == 10
    assert len(list((tmp_path / "knowledge").glob("*.md"))) == 10
    with connection_factory() as connection:
        assert connection.execute(
            """
            SELECT count(*) AS count
            FROM video_knowledge_documents document
            JOIN video_ingestion_batch_items item ON item.job_id=document.job_id
            WHERE item.batch_id=%s AND document.ingestion_status='completed'
            """,
            (batch["id"],),
        ).fetchone()["count"] == 10
    assert repository.audit_batch_consistency() == []


def test_failures_and_restart_reconciliation_are_idempotent(
    repository_schema,
) -> None:
    repository, connection_factory = repository_schema
    discovery_id = seed_discovery(connection_factory, works=2)
    batch = repository.confirm_discovery(
        discovery_id, "batch-user", Destination.MAIN
    )
    first = repository.claim_next()
    second = repository.claim_next()
    assert first and second

    # Simulate recover_stale having returned a running job to RECEIVED before
    # the process died, leaving its item mirror stale.
    with connection_factory() as connection:
        connection.execute(
            """
            UPDATE video_ingestion_jobs
            SET state='RECEIVED', processing_started_at=NULL,
                last_error_code='WORKER_RESTART'
            WHERE id=%s
            """,
            (first["id"],),
        )
        connection.execute(
            """
            UPDATE video_ingestion_jobs
            SET state='FAILED', processing_finished_at=now(),
                last_error_code='RETRY_EXHAUSTED'
            WHERE id=%s
            """,
            (second["id"],),
        )

    recovered = repository.reconcile_batches()
    assert recovered["repaired_items"] == 2
    facts = get_batch_facts(connection_factory, batch["id"])
    assert facts["batch"]["state"] == "RUNNING"
    assert facts["batch"]["queued_count"] == 1
    assert facts["batch"]["failed_count"] == 1
    assert [item["state"] for item in facts["items"]] == ["QUEUED", "FAILED"]

    claimed = repository.claim_next()
    assert claimed and claimed["id"] == first["id"]
    completed = repository.complete_batch_child(first["id"], uuid.uuid4())
    assert completed["batch"]["state"] == "COMPLETED_WITH_ERRORS"
    assert completed["batch"]["completed_count"] == 1
    assert completed["batch"]["failed_count"] == 1
    second_pass = repository.reconcile_batches()
    assert second_pass["repaired_items"] == 0


def test_illegal_transition_rolls_back_without_partial_updates(
    repository_schema,
) -> None:
    repository, connection_factory = repository_schema
    discovery_id = seed_discovery(connection_factory, works=1)
    batch = repository.confirm_discovery(
        discovery_id, "batch-user", Destination.MAIN
    )
    job = get_batch_facts(connection_factory, batch["id"])["jobs"][0]

    with pytest.raises(BatchStateError):
        repository.complete_batch_child(job["id"], uuid.uuid4())
    facts = get_batch_facts(connection_factory, batch["id"])
    assert facts["batch"]["state"] == "QUEUED"
    assert facts["batch"]["queued_count"] == 1
    assert facts["items"][0]["state"] == "QUEUED"
    assert facts["jobs"][0]["state"] == "RECEIVED"

    # Auth/rate-limit pauses require the caller to explicitly authorize resume
    # after its platform probe succeeds.
    paused = repository.pause_batch(
        batch["id"],
        "batch-user",
        state=BatchState.PAUSED_AUTH,
        code="AUTH_EXPIRED",
    )
    assert paused["state"] == "PAUSED_AUTH"
    with pytest.raises(BatchStateError):
        repository.resume_batch(batch["id"], "batch-user")
    resumed = repository.resume_batch(
        batch["id"],
        "batch-user",
        allowed_states=(BatchState.PAUSED_AUTH,),
    )
    assert resumed["state"] == "QUEUED"


def test_confirmation_failure_rolls_back_batch_items_and_jobs(
    repository_schema, monkeypatch
) -> None:
    repository, connection_factory = repository_schema
    discovery_id = seed_discovery(connection_factory, works=2)
    calls = 0
    original = repository._find_completed_document

    def fail_after_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected transaction failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, "_find_completed_document", fail_after_first)
    with pytest.raises(RuntimeError, match="injected transaction failure"):
        repository.confirm_discovery(
            discovery_id, "batch-user", Destination.MAIN
        )

    with connection_factory() as connection:
        assert connection.execute(
            "SELECT count(*) AS count FROM video_ingestion_batches"
        ).fetchone()["count"] == 0
        assert connection.execute(
            "SELECT count(*) AS count FROM video_ingestion_batch_items"
        ).fetchone()["count"] == 0
        assert connection.execute(
            """
            SELECT count(*) AS count FROM video_ingestion_jobs
            WHERE notification_mode='BATCH_SILENT'
            """
        ).fetchone()["count"] == 0
        discovery = connection.execute(
            "SELECT state FROM video_author_discoveries WHERE id=%s",
            (discovery_id,),
        ).fetchone()
        assert discovery["state"] == "READY"


def test_confirmation_enforces_duration_budget_and_locked_destination(
    repository_schema,
) -> None:
    repository, connection_factory = repository_schema
    discovery_id = seed_discovery(connection_factory, works=2)
    with connection_factory() as connection:
        connection.execute(
            """
            UPDATE video_author_discovery_items
            SET duration_seconds=NULL WHERE discovery_id=%s
            """,
            (discovery_id,),
        )
    limited_settings = Settings(
        enforce_batch_estimated_duration_budget=True,
        max_batch_estimated_duration_seconds=7199,
        max_batch_unknown_duration_reservation_seconds=3600,
    )
    limited = SqlRepository(
        limited_settings, connection_factory=connection_factory
    )
    with pytest.raises(BatchBudgetExceededError) as error:
        limited.confirm_discovery(discovery_id, "batch-user", Destination.MAIN)
    assert error.value.estimated_seconds == 7200
    with connection_factory() as connection:
        assert connection.execute(
            "SELECT count(*) AS count FROM video_ingestion_batches"
        ).fetchone()["count"] == 0

    created = repository.confirm_discovery(
        discovery_id, "batch-user", Destination.MAIN, limit=1
    )
    facts = get_batch_facts(connection_factory, created["id"])
    assert facts["batch"]["selection_policy"]["estimated_duration_seconds"] == 3600
    job = facts["jobs"][0]
    assert (
        repository.select_destination(
            str(job["id"]), "batch-user", Destination.COOKING
        )
        == "locked"
    )
    assert repository.get_job(str(job["id"]))["selected_destination"] == "main"


def test_all_preview_selection_is_preserved_and_must_match_preview(
    repository_schema,
) -> None:
    repository, connection_factory = repository_schema
    discovery_id = seed_discovery(connection_factory, works=20)
    with pytest.raises(ValueError, match="every eligible preview item"):
        repository.validate_discovery_selection(
            discovery_id,
            "batch-user",
            10,
            "all_preview",
        )

    repository.validate_discovery_selection(
        discovery_id,
        "batch-user",
        20,
        "all_preview",
    )
    batch = repository.confirm_discovery(
        discovery_id,
        "batch-user",
        Destination.MAIN,
        limit=20,
        selection_kind="all_preview",
    )
    facts = get_batch_facts(connection_factory, batch["id"])
    assert facts["batch"]["selection_policy"]["kind"] == "all_preview"
    assert facts["batch"]["selection_policy"]["limit"] == 20
    assert facts["batch"]["selection_policy"]["preview_eligible_count"] == 20
    assert not facts["batch"]["selection_policy"]["duration_budget_enforced"]


def test_all_preview_can_create_more_than_one_hundred_items(
    repository_schema,
) -> None:
    repository, connection_factory = repository_schema
    discovery_id = seed_discovery(connection_factory, works=137)
    batch = repository.confirm_discovery(
        discovery_id,
        "batch-user",
        Destination.MAIN,
        limit=137,
        selection_kind="all_preview",
    )
    facts = get_batch_facts(connection_factory, batch["id"])
    assert facts["batch"]["total_count"] == 137
    assert facts["batch"]["selection_policy"]["limit"] == 137
    assert len(facts["items"]) == 137
    assert len(facts["jobs"]) == 137


def test_preview_selection_revalidates_owner_state_expiry_and_limit(
    repository_schema,
) -> None:
    repository, connection_factory = repository_schema
    discovery_id = seed_discovery(connection_factory, works=2)
    selected = repository.validate_discovery_selection(
        discovery_id, "batch-user", 2
    )
    assert selected["state"] == "READY"
    with pytest.raises(BatchStateError):
        repository.validate_discovery_selection(
            repository.confirm_discovery(
                discovery_id, "batch-user", Destination.MAIN
            )["discovery_id"],
            "batch-user",
            2,
        )
    other = seed_discovery(connection_factory, user_id="other-user", works=1)
    with pytest.raises(Exception):
        repository.validate_discovery_selection(other, "batch-user", 1)
    with pytest.raises(ValueError):
        repository.validate_discovery_selection(other, "other-user", 2)


def test_pause_claim_and_complete_cancel_races_preserve_invariants(
    repository_schema,
) -> None:
    repository, connection_factory = repository_schema
    discovery_id = seed_discovery(connection_factory, works=1)
    batch = repository.confirm_discovery(
        discovery_id, "batch-user", Destination.MAIN
    )
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait()
        return repository.claim_next()

    def pause():
        barrier.wait()
        return repository.pause_batch(batch["id"], "batch-user")

    with ThreadPoolExecutor(max_workers=2) as executor:
        claim_future = executor.submit(claim)
        pause_future = executor.submit(pause)
        claimed = claim_future.result()
        paused = pause_future.result()
    assert paused["state"] == "PAUSED_USER"
    facts = get_batch_facts(connection_factory, batch["id"])
    if claimed:
        assert facts["items"][0]["state"] == "RUNNING"
        assert facts["batch"]["running_count"] == 1
    else:
        assert facts["items"][0]["state"] == "QUEUED"
        assert facts["batch"]["queued_count"] == 1

    repository.resume_batch(batch["id"], "batch-user")
    child = claimed or repository.claim_next()
    assert child
    finish_barrier = threading.Barrier(2)

    def complete():
        finish_barrier.wait()
        return repository.complete_batch_child(child["id"], uuid.uuid4())

    def cancel():
        finish_barrier.wait()
        return repository.request_batch_cancel(batch["id"], "batch-user")

    with ThreadPoolExecutor(max_workers=2) as executor:
        completed_future = executor.submit(complete)
        cancelled_future = executor.submit(cancel)
        completed_future.result()
        cancelled_future.result()
    facts = get_batch_facts(connection_factory, batch["id"])
    assert facts["items"][0]["state"] == "COMPLETED"
    assert facts["batch"]["state"] == "COMPLETED"
    assert facts["batch"]["completed_count"] == 1
    assert facts["batch"]["cancelled_count"] == 0
