"""Isolated PostgreSQL session-gate contracts; no real account requests."""

from copy import deepcopy

from test_stage_queue_integration import claim
from test_stage_queue_integration import isolated as isolated_fixture

isolated = isolated_fixture

from rag_favorite import video_pipeline as pipeline
from rag_favorite.queue_control import read_control, set_control
from rag_favorite.xhs_session_policy import SessionPolicy, failure
from rag_favorite.xhs_session_service import DatabaseMirror

URL = "https://www.xiaohongshu.com/explore/" + "a" * 24


def test_budget_wait_releases_stage_and_does_not_count_failure(isolated):
    s = isolated
    job_id = s.add(payload={"source_url": URL})
    s.stage(job_id)
    with claim(s) as (job, owner, _):
        pipeline.fail_stage(
            s.config,
            s.video,
            job,
            "download",
            owner,
            failure("XHS_DEFERRED", delay=1800),
            0.1,
        )
    assert s.execute(
        "SELECT status,attempts,owner,retry_at>now()+interval '29 minutes' FROM rag_video_pipeline WHERE job_id=%s",
        (job_id,),
    )[0] == ("queued", 0, None, True)
    assert (
        s.execute("SELECT attempts FROM rag_video_jobs WHERE id=%s", (job_id,))[0][0]
        == 0
    )
    with claim(s) as (job, _, _guard):
        assert job is None


def test_path_gate_and_recovery_preserve_manual_pauses(isolated):
    s = isolated
    job_id = s.add(state="blocked", payload={"source_url": URL})
    s.stage(job_id, status="blocked")
    s.execute(
        "UPDATE rag_video_jobs SET error_code='XHS_LOGIN_OR_VERIFICATION_REQUIRED' WHERE id=%s",
        (job_id,),
    )
    s.execute(
        "UPDATE rag_video_pipeline SET error_code='XHS_LOGIN_OR_VERIFICATION_REQUIRED' WHERE job_id=%s",
        (job_id,),
    )
    set_control(s.video, paused=True, job_id=job_id, job_paused=True)
    policy = SessionPolicy(s.video.root.parent / "test-session")
    mirror = DatabaseMirror(s.config, s.video)
    policy.failed("note", failure("XHS_VERIFICATION_REQUIRED"))
    mirror.publish(policy)
    policy.success("owner")
    policy.success("favorites")
    mirror.publish(policy)
    assert s.execute(
        "SELECT xhs_download_blocked,xhs_login_required FROM rag_video_pipeline_control WHERE library_id=%s",
        (s.lid,),
    )[0] == (True, False)
    policy.success("note")
    mirror.publish(policy)
    import pytest

    from rag_favorite.video_provider import ProviderUnavailable

    with pytest.raises(ProviderUnavailable, match="ACCEPTANCE_PAUSED"):
        mirror.recovered(policy)
    policy.data["recovery"]["phase"] = "observation"
    mirror.recovered(policy)
    assert (
        s.execute("SELECT state FROM rag_video_jobs WHERE id=%s", (job_id,))[0][0]
        == "queued"
    )
    assert read_control(s.video).paused
    assert job_id in read_control(s.video).paused_jobs
    with claim(s) as (job, _, _guard):
        assert job is None


def test_stale_session_event_cannot_release_new_gate(isolated):
    s = isolated
    policy = SessionPolicy(s.video.root.parent / "test-session")
    mirror = DatabaseMirror(s.config, s.video)
    policy.success("note")
    old = deepcopy(policy.data)
    policy.failed("note", failure("XHS_VERIFICATION_REQUIRED"))
    mirror.publish(policy)
    policy.data = old
    mirror.publish(policy)
    assert (
        s.execute(
            "SELECT xhs_download_blocked FROM rag_video_pipeline_control WHERE library_id=%s",
            (s.lid,),
        )[0][0]
        is True
    )


def test_canary_excludes_preexisting_source_token_failure(isolated):
    s = isolated
    bad = s.add(state="blocked", payload={"source_url": URL})
    s.execute(
        "UPDATE rag_video_jobs SET error_code='XHS_SOURCE_TOKEN_REFRESH_UNAVAILABLE' WHERE id=%s",
        (bad,),
    )
    mirror = DatabaseMirror(s.config, s.video)
    assert mirror.candidate() is None
    good_url = "https://www.xiaohongshu.com/explore/" + "b" * 24
    s.add(payload={"source_url": good_url})
    assert mirror.candidate() == good_url


def test_acceptance_gate_admits_only_three_jobs_and_other_sources(isolated):
    s = isolated
    ids = [s.add(payload={"source_url": URL}) for _ in range(4)]
    other = s.add(payload={"source_url": "https://www.bilibili.com/video/BVfixture"})
    for ident in [*ids, other]:
        s.stage(ident)
    policy = SessionPolicy(s.video.root.parent / "test-session")
    policy.data["recovery"].update(phase="acceptance", allowed_jobs=ids[:3])
    policy.data["network"] = {"paused": False}
    policy.save()
    mirror = DatabaseMirror(s.config, s.video)
    mirror.publish(policy)
    admitted = s.execute(
        "SELECT j.id::text FROM rag_video_jobs j WHERE j.library_id=%s AND "
        + pipeline.XHS_GATE_SQL,
        (s.lid,),
    )
    assert {r[0] for r in admitted} == {*ids[:3], other}
    policy.failed("favorites", failure("XHS_PLATFORM_BLOCKED", platform=300012))
    mirror.publish(policy)
    admitted = s.execute(
        "SELECT j.id::text FROM rag_video_jobs j WHERE j.library_id=%s AND "
        + pipeline.XHS_GATE_SQL,
        (s.lid,),
    )
    assert {r[0] for r in admitted} == {other}


def test_publication_acceptance_uses_real_stage_and_versioned_pagination(
    isolated, monkeypatch
):
    from rag_favorite import knowledge_summaries, video_retrieval
    from rag_favorite.xhs_acceptance import verify_publication

    s = isolated
    ident = s.add(
        state="complete", payload={"source_url": URL, "title": "fixture title"}
    )
    receipt = {"media_validated": True, "note_id": "a" * 24, "kind": "video"}
    assert verify_publication(s.config, s.video, ident, receipt) is None
    s.stage(ident, name="publish", status="done")
    calls = []
    monkeypatch.setattr(
        video_retrieval,
        "search_all",
        lambda *args: [
            {
                "document_id": "video:" + ident,
                "metadata": {"version": "fixture-version"},
            }
        ],
    )

    def read(*args, **kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "version": "fixture-version",
            "complete": kwargs["offset"] == 10,
            "next_offset": 10,
            "total_characters": 20,
        }

    monkeypatch.setattr(knowledge_summaries, "document_context", read)
    result = verify_publication(s.config, s.video, ident, receipt)
    assert result["complete"] and result["pages"] == 2
    assert [c["offset"] for c in calls] == [0, 10]
    assert all(c["version"] == "fixture-version" for c in calls)
    monkeypatch.setattr(video_retrieval, "search_all", lambda *args: [])
    assert verify_publication(s.config, s.video, ident, receipt) is None
