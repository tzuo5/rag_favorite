from __future__ import annotations

import asyncio
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from dotenv import dotenv_values
from psycopg import sql
from psycopg.rows import dict_row

from backend.ingestion.config import Settings
from backend.ingestion.discovery.models import (
    AuthorDiscoveryResult,
    AuthorSnapshot,
    DiscoveredWork,
)
from backend.ingestion.discovery_worker import AuthorDiscoveryWorker
from backend.ingestion.models import (
    DiscoveryEligibility,
    Platform,
    WorkContentType,
)
from backend.ingestion.repository import SqlRepository


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.skipif(
        os.getenv("RUN_AUTHOR_DISCOVERY_TESTS") != "1",
        reason="set RUN_AUTHOR_DISCOVERY_TESTS=1 for isolated discovery tests",
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
def discovery_schema():
    schema = f"test_author_discovery_{uuid.uuid4().hex}"
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
            "0004_multiplatform_author_batch.sql",
            "0005_explicit_knowledge_destinations.sql",
            "0006_unlimited_author_batches.sql",
            "0007_work_controls.sql",
        ):
            admin.execute((MIGRATIONS / migration).read_text(encoding="utf-8"))

    def connection_factory() -> psycopg.Connection:
        return psycopg.connect(
            **parameters, options=f"-csearch_path={schema},public"
        )

    repository = SqlRepository(Settings(), connection_factory=connection_factory)
    try:
        yield repository, connection_factory
    finally:
        with psycopg.connect(**parameters, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def result_for(canonical_url: str, count: int = 2) -> AuthorDiscoveryResult:
    return AuthorDiscoveryResult(
        author=AuthorSnapshot(
            platform=Platform.YOUTUBE,
            author_id="UC_DISCOVERY_TEST",
            canonical_url=canonical_url,
            display_name="Discovery Test",
        ),
        works=tuple(
            DiscoveredWork(
                source_id=f"testvideo0{index}",
                canonical_url=(
                    f"https://www.youtube.com/watch?v=testvideo0{index}"
                ),
                title=f"Video {index}",
                published_at=datetime(2026, 7, index, tzinfo=timezone.utc),
                duration_seconds=60.0,
                content_type=WorkContentType.UNKNOWN,
                position=index,
                eligibility=DiscoveryEligibility.ELIGIBLE,
                raw_metadata={"id": f"testvideo0{index}"},
            )
            for index in range(1, count + 1)
        ),
        extractor_name="YoutubeTab",
        truncated=False,
    )


def create(repository: SqlRepository, suffix: str = "") -> dict:
    canonical = f"https://www.youtube.com/@discovery{suffix}/videos"
    return repository.create_author_discovery(
        user_id="discovery-user",
        chat_id="discovery-chat",
        message_id=f"message{suffix}",
        input_url=canonical,
        platform=Platform.YOUTUBE,
        canonical_author_url=canonical,
    )


def test_discovery_queue_is_idempotent_and_completion_is_atomic(
    discovery_schema,
) -> None:
    repository, connection_factory = discovery_schema
    created = create(repository)
    replay = create(repository)
    assert replay["id"] == created["id"]
    claimed = repository.claim_author_discovery()
    assert claimed["id"] == created["id"]
    ready = repository.complete_author_discovery(
        created["id"], result_for(created["canonical_author_url"])
    )
    assert ready["state"] == "READY"
    assert ready["discovered_count"] == 2
    assert ready["eligible_count"] == 2
    with connection_factory() as connection:
        rows = connection.execute(
            """
            SELECT * FROM video_author_discovery_items
            WHERE discovery_id=%s ORDER BY position
            """,
            (created["id"],),
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["raw_metadata"] == {"id": "testvideo01"}

    other = create(repository, "atomic")
    repository.claim_author_discovery()
    bad = result_for(other["canonical_author_url"])
    bad = AuthorDiscoveryResult(
        author=bad.author,
        works=(bad.works[0], bad.works[0]),
        extractor_name=bad.extractor_name,
        truncated=False,
    )
    with pytest.raises(ValueError, match="duplicate source"):
        repository.complete_author_discovery(other["id"], bad)
    with connection_factory() as connection:
        state = connection.execute(
            "SELECT state FROM video_author_discoveries WHERE id=%s",
            (other["id"],),
        ).fetchone()["state"]
        count = connection.execute(
            """
            SELECT count(*) AS count FROM video_author_discovery_items
            WHERE discovery_id=%s
            """,
            (other["id"],),
        ).fetchone()["count"]
    assert state == "DISCOVERING"
    assert count == 0


def test_stale_recovery_expiry_and_safe_failure(discovery_schema) -> None:
    repository, connection_factory = discovery_schema
    stale = create(repository, "stale")
    repository.claim_author_discovery()
    assert repository.recover_stale_author_discoveries(0) == 1
    assert repository.get_author_discovery(stale["id"])["state"] == "QUEUED"

    failed = repository.claim_author_discovery()
    repository.fail_author_discovery(
        failed["id"], "DISCOVERY_FAILED", "安全的错误摘要"
    )
    failed_row = repository.get_author_discovery(failed["id"])
    assert failed_row["state"] == "FAILED"
    assert failed_row["error_message"] == "安全的错误摘要"

    expiring = create(repository, "expire")
    with connection_factory() as connection:
        connection.execute(
            """
            UPDATE video_author_discoveries
            SET created_at=now() - interval '2 minutes',
                expires_at=now() - interval '1 second'
            WHERE id=%s
            """,
            (expiring["id"],),
        )
    assert repository.expire_author_discoveries() == 1
    assert repository.get_author_discovery(expiring["id"])["state"] == "EXPIRED"


def test_discovery_can_pause_resume_and_cancel_without_worker_race(
    discovery_schema,
) -> None:
    repository, _ = discovery_schema
    created = create(repository, "controls")
    paused = repository.pause_author_discovery(
        created["id"], "discovery-user"
    )
    assert paused["state"] == "PAUSED_USER"
    assert repository.claim_author_discovery() is None

    resumed = repository.resume_author_discovery(
        created["id"], "discovery-user"
    )
    assert resumed["state"] == "QUEUED"
    claimed = repository.claim_author_discovery()
    assert claimed["id"] == created["id"]

    paused = repository.pause_author_discovery(
        created["id"], "discovery-user"
    )
    stale_result = repository.complete_author_discovery(
        created["id"], result_for(created["canonical_author_url"])
    )
    assert stale_result["state"] == "PAUSED_USER"

    repository.resume_author_discovery(
        created["id"], "discovery-user"
    )
    cancelled = repository.cancel_author_discovery(
        created["id"], "discovery-user"
    )
    assert cancelled["state"] == "CANCELLED"
    assert cancelled["finished_at"] is not None


def test_platform_gate_serializes_slots_and_half_open_probe(
    discovery_schema,
) -> None:
    repository, _ = discovery_schema
    start = datetime.now(timezone.utc) + timedelta(seconds=1)
    barrier = threading.Barrier(2)

    def acquire():
        barrier.wait()
        return repository.acquire_platform_request(
            Platform.YOUTUBE, now=start, jitter_seconds=0
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        leases = list(executor.map(lambda _: acquire(), range(2)))
    assert sum(lease.allowed for lease in leases) == 1
    assert {lease.reason for lease in leases if not lease.allowed} == {
        "REQUEST_INTERVAL"
    }

    failure_time = start + timedelta(seconds=10)
    for index in range(3):
        gate = repository.record_platform_failure(
            Platform.YOUTUBE,
            "RATE_LIMITED",
            rate_limited=True,
            now=failure_time + timedelta(seconds=index),
            jitter_seconds=0,
        )
    assert gate["circuit_state"] == "OPEN"
    assert gate["blocked_until"] is not None
    probe_time = gate["blocked_until"] + timedelta(seconds=1)
    assert repository.acquire_platform_request(
        Platform.YOUTUBE, now=probe_time
    ).reason == "PROBE_REQUIRED"
    first_probe = repository.acquire_platform_request(
        Platform.YOUTUBE, probe=True, now=probe_time
    )
    second_probe = repository.acquire_platform_request(
        Platform.YOUTUBE, probe=True, now=probe_time
    )
    assert first_probe.allowed is True
    assert second_probe.allowed is False
    assert second_probe.reason == "PROBE_IN_FLIGHT"
    closed = repository.record_platform_success(
        Platform.YOUTUBE, now=probe_time
    )
    assert closed["circuit_state"] == "CLOSED"
    assert closed["consecutive_failures"] == 0


def test_auth_failure_opens_indefinite_circuit(discovery_schema) -> None:
    repository, _ = discovery_schema
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    gate = repository.record_platform_failure(
        Platform.YOUTUBE,
        "AUTH_EXPIRED",
        immediate_open=True,
        now=now,
    )
    assert gate["circuit_state"] == "OPEN"
    assert gate["blocked_until"] is None
    lease = repository.acquire_platform_request(
        Platform.YOUTUBE, probe=True, now=now + timedelta(days=1)
    )
    assert lease.allowed is True
    assert lease.probe is True
    duplicate = repository.acquire_platform_request(
        Platform.YOUTUBE, probe=True, now=now + timedelta(days=1)
    )
    assert duplicate.allowed is False
    assert duplicate.reason == "PROBE_IN_FLIGHT"


class FakeAdapter:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def discover(self, url, *, scan_limit):
        self.calls += 1
        return self.result


def test_worker_persists_preview_without_creating_jobs(discovery_schema) -> None:
    repository, connection_factory = discovery_schema
    created = create(repository, "worker")
    adapter = FakeAdapter(result_for(created["canonical_author_url"]))
    worker = AuthorDiscoveryWorker(repository, adapter)
    completed = asyncio.run(worker.run_once())
    assert completed["state"] == "READY"
    assert adapter.calls == 1
    with connection_factory() as connection:
        facts = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM video_ingestion_jobs) AS jobs,
              (SELECT count(*) FROM video_ingestion_batches) AS batches,
              (SELECT count(*) FROM video_author_discovery_items) AS items
            """
        ).fetchone()
    assert facts == {"jobs": 0, "batches": 0, "items": 2}


def test_multiplatform_discovery_never_persists_transient_profile_query(
    discovery_schema,
) -> None:
    repository, connection_factory = discovery_schema
    canonical = "https://space.bilibili.com/12345/video"
    created = repository.create_author_discovery(
        user_id="bili-user",
        chat_id="bili-chat",
        message_id="bili-message",
        input_url=f"{canonical}?xsec_token=must-not-persist",
        platform=Platform.BILIBILI,
        canonical_author_url=canonical,
        scan_limit=10,
    )
    claimed = repository.claim_author_discovery()
    assert claimed["id"] == created["id"]
    result = AuthorDiscoveryResult(
        author=AuthorSnapshot(
            platform=Platform.BILIBILI,
            author_id="12345",
            canonical_url=canonical,
            display_name="Bili Test",
        ),
        works=(
            DiscoveredWork(
                source_id="BV1xx411c7mD",
                canonical_url="https://www.bilibili.com/video/BV1xx411c7mD",
                title="Bili Video",
                published_at=None,
                duration_seconds=60,
                content_type=WorkContentType.VIDEO,
                position=1,
                eligibility=DiscoveryEligibility.ELIGIBLE,
                raw_metadata={"id": "BV1xx411c7mD"},
            ),
        ),
        extractor_name="BilibiliSpaceVideo",
        truncated=False,
    )
    ready = repository.complete_author_discovery(created["id"], result)
    assert ready["platform"] == "bilibili"
    with connection_factory() as connection:
        stored = connection.execute(
            """
            SELECT input_url, canonical_author_url
            FROM video_author_discoveries WHERE id=%s
            """,
            (created["id"],),
        ).fetchone()
    assert stored == {
        "input_url": canonical,
        "canonical_author_url": canonical,
    }
    assert "xsec" not in repr(stored)
