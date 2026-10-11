"""Opt-in durable stage queue checks, using only fixture jobs and no models."""

import json
import os
import threading
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from rag_favorite import video_pipeline as pipeline
from rag_favorite.config import load_config
from rag_favorite.database import connect_database
from rag_favorite.pipeline_fence import (
    OwnershipLost,
    PipelinePaused,
    TemporaryBudgetExceeded,
    assert_owner,
    stage_owner,
)
from rag_favorite.queue_control import set_control
from rag_favorite.video_config import VideoConfig
from rag_favorite.video_provider import ProviderUnavailable
from rag_favorite.video_store import library_id


@pytest.fixture
def isolated(tmp_path):
    path = os.environ.get("RAG_GUI_TEST_CONFIG")
    if not path:
        pytest.skip("Set RAG_GUI_TEST_CONFIG for isolated live stage-queue checks")
    config = load_config(path)
    video = VideoConfig(
        root=tmp_path / "stage-queue-owned",
        credentials_file=tmp_path / "unused-credentials.env",
        pipeline_enabled=True,
    )
    lid, ids = library_id(video), []
    with connect_database(config, register_pgvector=False) as c:
        assert c.execute("SELECT to_regclass('public.rag_video_pipeline')").fetchone()[
            0
        ], "Apply migration 0009 before live stage-queue checks"

    def add(*, state="queued", priority=10, foreign=False, payload=None):
        job_id = str(uuid4())
        ids.append(job_id)
        with connect_database(config, register_pgvector=False) as c:
            c.execute(
                "INSERT INTO public.rag_video_jobs(id,library_id,collection,state,stage,payload,priority) "
                "VALUES(%s,%s,'cooking',%s,'queued',%s::jsonb,%s)",
                (
                    job_id,
                    "stage-queue-foreign-" + job_id if foreign else lid,
                    state,
                    json.dumps(payload or {"title": "isolated stage fixture"}),
                    priority,
                ),
            )
        return job_id

    def stage(job_id, name="download", *, status="queued", foreign=False):
        with connect_database(config, register_pgvector=False) as c:
            c.execute(
                "INSERT INTO public.rag_video_pipeline(job_id,library_id,stage,status) "
                "VALUES(%s,%s,%s,%s)",
                (
                    job_id,
                    "stage-queue-foreign-" + job_id if foreign else lid,
                    name,
                    status,
                ),
            )

    def execute(query, args=()):
        with connect_database(config, register_pgvector=False) as c:
            result = c.execute(query, args)
            return result.fetchall() if result.description else result.rowcount

    try:
        yield SimpleNamespace(
            config=config,
            video=video,
            lid=lid,
            ids=ids,
            add=add,
            stage=stage,
            execute=execute,
        )
    finally:
        with connect_database(config, register_pgvector=False) as c:
            c.execute(
                "DELETE FROM public.rag_video_jobs WHERE id=ANY(%s::uuid[])", (ids,)
            )
            c.execute(
                "DELETE FROM public.rag_video_pipeline_control WHERE library_id=%s",
                (lid,),
            )


@contextmanager
def claim(isolated, stage="download", *, owner=None, video=None):
    owner = owner or "test-owner:" + uuid4().hex
    with connect_database(isolated.config, register_pgvector=False) as guard:
        guard.autocommit = True
        job = pipeline.claim_stage(
            isolated.config, video or isolated.video, stage, owner, guard
        )
        yield job, owner, guard


def row(isolated, job_id, stage="download"):
    values = isolated.execute(
        "SELECT status,owner,token,attempts,error_code,lease_until,elapsed_seconds,artifact_ref "
        "FROM public.rag_video_pipeline WHERE job_id=%s AND stage=%s",
        (job_id, stage),
    )[0]
    return dict(
        zip(
            (
                "status",
                "owner",
                "token",
                "attempts",
                "error",
                "lease_until",
                "elapsed",
                "artifact",
            ),
            values,
            strict=True,
        )
    )


def expire(isolated, job_id, stage="download"):
    isolated.execute(
        "UPDATE public.rag_video_pipeline SET lease_until=now()-interval '1 second' "
        "WHERE job_id=%s AND stage=%s",
        (job_id, stage),
    )


def test_claim_is_owner_scoped_prioritized_and_fenced(isolated):
    low = isolated.add(priority=10)
    high = isolated.add(priority=40)
    foreign = isolated.add(priority=100, foreign=True)
    isolated.stage(low)
    isolated.stage(high)
    isolated.stage(foreign, foreign=True)
    with claim(isolated) as (job, owner, _):
        assert job["id"] == high and job["attempts"] == 1
        lease = pipeline.Lease(
            isolated.config, isolated.video, job, "download", owner, threading.Event()
        )
        with stage_owner(lease), connect_database(isolated.config) as c:
            assert_owner(c)
        assert row(isolated, high)["status"] == "running"
        assert row(isolated, low)["status"] == "queued"
        assert row(isolated, foreign)["status"] == "queued"
        with claim(isolated) as (other, _, _):
            assert other["id"] == low
    isolated.execute(
        "UPDATE public.rag_video_pipeline SET token=token+1 WHERE job_id=%s AND stage='download'",
        (high,),
    )
    with pytest.raises(OwnershipLost), stage_owner(lease):
        assert_owner()


def test_expired_recovery_skips_active_advisory_lock_and_rejects_old_owner(isolated):
    job_id = isolated.add()
    isolated.stage(job_id)
    with claim(isolated) as (job, owner, _):
        lease = pipeline.Lease(
            isolated.config, isolated.video, job, "download", owner, threading.Event()
        )
        expire(isolated, job_id)
        assert pipeline.recover_leases(isolated.config, isolated.video) == 0
        assert row(isolated, job_id)["status"] == "running"
        with pytest.raises(OwnershipLost):
            lease.assert_live()
    assert pipeline.recover_leases(isolated.config, isolated.video) == 1
    recovered = row(isolated, job_id)
    assert recovered["status"] == "queued" and recovered["owner"] is None
    assert recovered["token"] > job["token"]
    with claim(isolated) as (new_job, new_owner, _):
        assert new_job["token"] > recovered["token"]
        with pytest.raises(OwnershipLost):
            pipeline.finish_stage(
                isolated.config, isolated.video, job, "download", owner, 1.0
            )
        current = row(isolated, job_id)
        assert current["owner"] == new_owner and current["status"] == "running"
        assert (
            isolated.execute(
                "SELECT count(*) FROM public.rag_video_pipeline WHERE job_id=%s AND stage='prepare'",
                (job_id,),
            )[0][0]
            == 0
        )


def test_expired_heartbeat_cannot_revive_the_claim(isolated, monkeypatch):
    job_id = isolated.add()
    isolated.stage(job_id)
    monkeypatch.setattr(pipeline, "HEARTBEAT_SECONDS", 0.001)
    with claim(isolated) as (job, owner, _):
        lease = pipeline.Lease(
            isolated.config, isolated.video, job, "download", owner, threading.Event()
        )
        expire(isolated, job_id)
        deadline = row(isolated, job_id)["lease_until"]
        lease.heartbeat()
        assert lease.lost.is_set()
        assert row(isolated, job_id)["lease_until"] == deadline


def test_finish_handoff_is_atomic_and_duplicate_finish_is_harmless(isolated):
    job_id = isolated.add()
    isolated.stage(job_id)
    with claim(isolated) as (job, owner, _):
        pipeline.finish_stage(
            isolated.config, isolated.video, job, "download", owner, 1.25
        )
        with pytest.raises(OwnershipLost):
            pipeline.finish_stage(
                isolated.config, isolated.video, job, "download", owner, 2.0
            )
    done = row(isolated, job_id)
    assert done["status"] == "done" and done["elapsed"] == 1.25
    assert done["owner"] is None and done["lease_until"] is None
    assert done["artifact"] == "derived/" + job_id
    assert isolated.execute(
        "SELECT stage,status FROM public.rag_video_pipeline WHERE job_id=%s ORDER BY stage",
        (job_id,),
    ) == [("download", "done"), ("prepare", "queued")]
    with claim(isolated, "prepare") as (next_job, _, _):
        assert next_job["id"] == job_id


def test_expired_claim_cannot_finish_or_enqueue_next_stage(isolated):
    job_id = isolated.add()
    isolated.stage(job_id)
    with claim(isolated) as (job, owner, _):
        expire(isolated, job_id)
        with pytest.raises(OwnershipLost):
            pipeline.finish_stage(
                isolated.config, isolated.video, job, "download", owner, 1.0
            )
    assert row(isolated, job_id)["status"] == "running"
    assert (
        isolated.execute(
            "SELECT count(*) FROM public.rag_video_pipeline WHERE job_id=%s", (job_id,)
        )[0][0]
        == 1
    )


def test_http503_retries_at_five_then_thirty_seconds_and_exhausts(isolated):
    job_id = isolated.add()
    isolated.stage(job_id, "llm")
    error = ProviderUnavailable(
        "upstream unavailable", code="CCR_HTTP_503", status_code=503, retryable=True
    )
    for attempt, delay in [(1, 5), (2, 30), (3, None)]:
        with claim(isolated, "llm") as (job, owner, _):
            assert job["id"] == job_id and job["attempts"] == attempt
            pipeline.fail_stage(
                isolated.config, isolated.video, job, "llm", owner, error, 0.25
            )
        current = row(isolated, job_id, "llm")
        assert current["attempts"] == attempt and current["error"] == "CCR_HTTP_503"
        assert current["status"] == ("queued" if delay else "blocked")
        if delay:
            remaining = isolated.execute(
                "SELECT extract(epoch FROM retry_at-now()) FROM public.rag_video_pipeline "
                "WHERE job_id=%s AND stage='llm'",
                (job_id,),
            )[0][0]
            assert delay - 2 <= remaining <= delay
            with claim(isolated, "llm") as (premature, _, _):
                assert premature is None
            isolated.execute(
                "UPDATE public.rag_video_pipeline SET retry_at=now() WHERE job_id=%s AND stage='llm'",
                (job_id,),
            )
    assert (
        isolated.execute(
            "SELECT state FROM public.rag_video_jobs WHERE id=%s", (job_id,)
        )[0][0]
        == "blocked"
    )
    assert isolated.execute(
        "SELECT consecutive_failures,circuit_until>now() FROM public.rag_video_pipeline_control "
        "WHERE library_id=%s",
        (isolated.lid,),
    ) == [(3, True)]
    following = isolated.add()
    isolated.stage(following, "llm")
    with claim(isolated, "llm") as (during_cooldown, _, _):
        assert during_cooldown is None
    isolated.execute(
        "UPDATE public.rag_video_pipeline_control SET circuit_until=now()-interval '1 second' "
        "WHERE library_id=%s",
        (isolated.lid,),
    )
    with claim(isolated, "llm") as (probe, owner, _):
        assert probe["id"] == following
        pipeline.finish_stage(
            isolated.config, isolated.video, probe, "llm", owner, 0.25
        )
    assert isolated.execute(
        "SELECT consecutive_failures,circuit_until FROM public.rag_video_pipeline_control "
        "WHERE library_id=%s",
        (isolated.lid,),
    ) == [(0, None)]


def test_http507_blocks_only_its_task_without_identical_retry(isolated):
    job_id = isolated.add(priority=40)
    following = isolated.add(priority=10)
    isolated.stage(job_id, "llm")
    isolated.stage(following, "llm")
    with claim(isolated, "llm") as (job, owner, _):
        assert job["id"] == job_id
        pipeline.fail_stage(
            isolated.config,
            isolated.video,
            job,
            "llm",
            owner,
            ProviderUnavailable(
                "retry buffer exceeded",
                code="CCR_HTTP_507",
                status_code=507,
                retryable=False,
            ),
            0.25,
        )
    current = row(isolated, job_id, "llm")
    assert current["status"] == "blocked" and current["attempts"] == 1
    with claim(isolated, "llm") as (other, _, _):
        assert other["id"] == following
    assert isolated.execute(
        "SELECT consecutive_failures,circuit_until FROM public.rag_video_pipeline_control WHERE library_id=%s",
        (isolated.lid,),
    )[0] == (1, None)


@pytest.mark.parametrize("status", [401, 507])
def test_persistent_link_errors_cool_down_without_replaying_tasks(isolated, status):
    jobs = [isolated.add(priority=40 - number) for number in range(4)]
    for job_id in jobs:
        isolated.stage(job_id, "llm")
    for job_id in jobs[:3]:
        with claim(isolated, "llm") as (job, owner, _):
            assert job["id"] == job_id
            pipeline.fail_stage(
                isolated.config,
                isolated.video,
                job,
                "llm",
                owner,
                ProviderUnavailable(
                    "persistent upstream error", status_code=status, retryable=False
                ),
                0.1,
            )
            assert row(isolated, job_id, "llm")["status"] == "blocked"
    with claim(isolated, "llm") as (probe, _, _):
        assert probe is None
    assert isolated.execute(
        "SELECT consecutive_failures,circuit_until>now() FROM public.rag_video_pipeline_control WHERE library_id=%s",
        (isolated.lid,),
    )[0] == (3, True)


@pytest.mark.parametrize(
    "failed_stage,error,target",
    [
        ("llm", "PIPELINE_PREPARATION_CHANGED", "prepare"),
        ("publish", "SUMMARY_GENERATION_MISMATCH", "llm"),
        ("publish", "SUMMARY_GENERATION_UNAVAILABLE", "llm"),
    ],
)
def test_manual_retry_rewinds_only_invalid_handoff(
    isolated, failed_stage, error, target
):
    from rag_favorite.config import ConfigError

    job_id = isolated.add()
    isolated.stage(job_id, failed_stage)
    with claim(isolated, failed_stage) as (job, owner, _):
        pipeline.fail_stage(
            isolated.config,
            isolated.video,
            job,
            failed_stage,
            owner,
            ConfigError(error),
            0.1,
        )
    assert row(isolated, job_id, failed_stage)["error"] == error
    isolated.execute(
        "UPDATE public.rag_video_jobs SET state='queued' WHERE id=%s", (job_id,)
    )
    pipeline.initialize_jobs(isolated.config, isolated.video)
    assert row(isolated, job_id, target)["status"] == "queued"
    with claim(isolated, failed_stage) as (stale, _, _):
        assert stale is None


def test_successful_request_resets_transport_failure_streak_under_live_fence(isolated):
    job_id = isolated.add()
    isolated.stage(job_id, "llm")
    isolated.execute(
        "INSERT INTO public.rag_video_pipeline_control(library_id,consecutive_failures) VALUES(%s,2)",
        (isolated.lid,),
    )
    with claim(isolated, "llm") as (job, owner, _):
        lease = pipeline.Lease(
            isolated.config, isolated.video, job, "llm", owner, threading.Event()
        )
        lease.request_succeeded()
        assert isolated.execute(
            "SELECT consecutive_failures FROM public.rag_video_pipeline_control WHERE library_id=%s",
            (isolated.lid,),
        ) == [(0,)]
        isolated.execute(
            "UPDATE public.rag_video_pipeline SET token=token+1 WHERE job_id=%s AND stage='llm'",
            (job_id,),
        )
        with pytest.raises(OwnershipLost):
            lease.request_succeeded()


def test_pause_stops_new_claims_and_preserves_attempt_budget(isolated):
    high = isolated.add(priority=40)
    low = isolated.add(priority=10)
    isolated.stage(high)
    isolated.stage(low)
    set_control(isolated.video, paused=True)
    assert pipeline.initialize_jobs(isolated.config, isolated.video) == 0
    with claim(isolated) as (paused, _, _):
        assert paused is None
    set_control(isolated.video, paused=False, job_id=high, job_paused=True)
    with claim(isolated) as (job, owner, _):
        assert job["id"] == low
        lease = pipeline.Lease(
            isolated.config, isolated.video, job, "download", owner, threading.Event()
        )
        set_control(isolated.video, job_id=low, job_paused=True)
        # Output may persist after pause; starting a fresh operation must stop.
        lease.assert_live()
        with pytest.raises(PipelinePaused):
            lease.before_operation()
        pipeline.fail_stage(
            isolated.config,
            isolated.video,
            job,
            "download",
            owner,
            PipelinePaused(),
            0.25,
        )
    current = row(isolated, low)
    assert current["status"] == "queued" and current["attempts"] == 0
    assert current["error"] == "PIPELINE_PAUSED"


def test_prepare_backpressure_caps_waiting_llm_jobs(isolated):
    for _ in range(isolated.video.pipeline_prefetch):
        isolated.stage(isolated.add(), "llm")
    preparing = isolated.add()
    isolated.stage(preparing, "prepare")
    with claim(isolated, "prepare") as (blocked, _, _):
        assert blocked is None
    with claim(isolated, "llm") as (active_llm, _, _):
        assert active_llm is not None
        with claim(isolated, "prepare") as (prepared, _, _):
            assert prepared["id"] == preparing


def test_admission_frontier_and_temp_budget_are_bounded(isolated):
    for number in range(8):
        isolated.add(priority=number)
    assert pipeline.initialize_jobs(isolated.config, isolated.video) == (
        isolated.video.pipeline_prefetch + 3
    )
    assert pipeline.initialize_jobs(isolated.config, isolated.video) == 0
    job_id = isolated.execute(
        "SELECT job_id FROM public.rag_video_pipeline WHERE library_id=%s LIMIT 1",
        (isolated.lid,),
    )[0][0]
    work = isolated.video.root / "work" / str(job_id)
    work.mkdir(parents=True)
    (work / "copy.bin").write_bytes(b"fixture-contents")
    limited = replace(isolated.video, pipeline_temp_bytes=8)
    assert pipeline.temporary_bytes(isolated.config, limited) == len(
        b"fixture-contents"
    )
    # Leave admission room, so the next refusal specifically proves disk gating.
    isolated.execute(
        "UPDATE public.rag_video_pipeline SET status='done' WHERE library_id=%s AND job_id<>%s",
        (isolated.lid, job_id),
    )
    assert pipeline.initialize_jobs(isolated.config, limited) == 0
    with claim(isolated, video=limited) as (job, owner, _):
        lease = pipeline.Lease(
            isolated.config, limited, job, "download", owner, threading.Event()
        )
        with pytest.raises(TemporaryBudgetExceeded):
            lease.before_operation()
    (work / "copy.bin").unlink()
    assert pipeline.initialize_jobs(isolated.config, limited) == 3


def test_terminal_phantom_rows_reconcile_after_active_guard_exits(isolated):
    complete = isolated.add(state="complete")
    duplicate = isolated.add(state="duplicate")
    queued_complete = isolated.add(state="complete")
    isolated.stage(complete, "llm", status="running")
    isolated.stage(duplicate, "download", status="blocked")
    isolated.stage(queued_complete, "prepare")
    with connect_database(isolated.config, register_pgvector=False) as guard:
        guard.autocommit = True
        guard.execute(
            "SELECT pg_advisory_lock(hashtext(%s))",
            (pipeline.stage_lock(complete, "llm"),),
        )
        pipeline.recover_leases(isolated.config, isolated.video)
        assert row(isolated, complete, "llm")["status"] == "running"
        assert row(isolated, duplicate)["status"] == "done"
        assert row(isolated, queued_complete, "prepare")["status"] == "done"
    pipeline.recover_leases(isolated.config, isolated.video)
    assert row(isolated, complete, "llm")["status"] == "done"
    snapshot = pipeline.pipeline_snapshot(isolated.config, isolated.video)
    assert snapshot["jobs"] == [] and snapshot["running"] == []
