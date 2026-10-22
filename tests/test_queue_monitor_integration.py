"""Opt-in live PostgreSQL checks in an isolated library; no model requests."""

import json
import os
from uuid import uuid4

import pytest

from rag_favorite import queue_monitor, video_retention, video_worker
from rag_favorite.config import ConfigError, load_config
from rag_favorite.database import connect_database
from rag_favorite.queue_control import read_control, set_control
from rag_favorite.queue_monitor import QueueMonitor
from rag_favorite.video_config import VideoConfig
from rag_favorite.video_store import library_id, update_job


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    path = os.environ.get("RAG_GUI_TEST_CONFIG")
    if not path:
        pytest.skip("Set RAG_GUI_TEST_CONFIG for isolated live database checks")
    config = load_config(path)
    video = VideoConfig(
        root=tmp_path / "gui-test-owned", credentials_file=tmp_path / "credentials.env"
    )
    video.root.mkdir()
    (video.root / "queue-worker.json").write_text(
        json.dumps({"pid": 123, "queue_control_version": 1})
    )
    monkeypatch.setattr(
        queue_monitor,
        "service_status",
        lambda: {"ActiveState": "active", "SubState": "running", "MainPID": "123"},
    )
    monkeypatch.setattr(queue_monitor, "worker_logs", lambda: ["isolated test log"])
    monkeypatch.setattr(video_worker, "local_encoder", lambda *args: {})
    monkeypatch.setattr(
        video_retention, "expire_failed_media", lambda *args, **kwargs: {}
    )
    ids = [str(uuid4()) for _ in range(4)]
    with connect_database(config, register_pgvector=False) as db:
        for number, job_id in enumerate(ids):
            db.execute(
                "INSERT INTO public.rag_video_jobs(id,library_id,collection,state,stage,payload,priority) VALUES(%s,%s,'cooking',%s,'queued',%s::jsonb,%s)",
                (
                    job_id,
                    library_id(video) if number < 3 else "gui-test-foreign-" + job_id,
                    "failed" if number == 2 else "queued",
                    json.dumps(
                        {
                            "title": "GUI fixture " + str(number),
                            "source_url": "https://example.invalid/?token=DO_NOT_EXPOSE",
                        }
                    ),
                    30 - number,
                ),
            )
    try:
        yield config, video, ids, QueueMonitor(config, video)
    finally:
        with connect_database(config, register_pgvector=False) as db:
            db.execute(
                "DELETE FROM public.rag_video_jobs WHERE id=ANY(%s::uuid[])", (ids,)
            )


def test_global_stop_blocks_new_claim_without_mutating_job_state(isolated):
    config, video, ids, monitor = isolated
    assert "暂停" in monitor.action("stop")
    assert video_worker.claim_next_job(config, video, encoder_ready=True) is None
    with connect_database(config, register_pgvector=False) as db:
        assert db.execute(
            "SELECT state,attempts FROM public.rag_video_jobs WHERE id=%s", (ids[0],)
        ).fetchone() == ("queued", 0)


def test_job_pause_skips_only_selected_task_and_resume_restores_it(isolated):
    config, video, ids, monitor = isolated
    monitor.action("stop_job", ids[0])
    assert video_worker.claim_next_job(config, video, encoder_ready=True)[
        0
    ] == uuid_value(ids[1])
    monitor.action("start_job", ids[0])
    assert video_worker.claim_next_job(config, video, encoder_ready=True)[
        0
    ] == uuid_value(ids[0])


def uuid_value(value):
    from uuid import UUID

    return UUID(value)


def test_stop_after_claim_allows_current_job_to_finish_and_blocks_next(isolated):
    config, video, ids, monitor = isolated
    assert (
        str(video_worker.claim_next_job(config, video, encoder_ready=True)[0]) == ids[0]
    )
    monitor.action("stop")
    update_job(config, video, ids[0], "complete", "complete")
    assert video_worker.claim_next_job(config, video, encoder_ready=True) is None
    assert monitor.snapshot()["counts"]["complete"] == 1


def test_retry_is_owner_scoped_and_keeps_global_pause(isolated):
    config, video, ids, monitor = isolated
    monitor.action("stop")
    monitor.action("start_job", ids[2])
    assert read_control(video).paused
    assert monitor.snapshot()["counts"]["queued"] == 3
    with pytest.raises(ConfigError, match="当前知识库"):
        monitor.action("stop_job", ids[3])
    assert video_worker.claim_next_job(config, video, encoder_ready=True) is None


def test_snapshot_is_live_owner_scoped_and_excludes_payload_secrets(isolated):
    _, video, ids, monitor = isolated
    set_control(video, job_id=ids[0], job_paused=True)
    snapshot = monitor.snapshot()
    assert snapshot["total"] == 3 and snapshot["paused_pending"] == 1
    assert ids[3] not in {job["id"] for job in snapshot["jobs"]}
    assert "DO_NOT_EXPOSE" not in json.dumps(snapshot)
    assert snapshot["control_supported"]


def test_once_worker_respects_pause_before_any_model_call(isolated, monkeypatch):
    config, video, ids, _ = isolated
    set_control(video, paused=True)

    def forbidden(*args):
        pytest.fail("Paused worker must not call a model")

    monkeypatch.setattr(video_worker, "local_encoder", forbidden)
    monkeypatch.setattr(video_worker, "process", forbidden)
    video_worker.run_worker(config, video, once=True)
    with connect_database(config, register_pgvector=False) as db:
        assert (
            db.execute(
                "SELECT attempts FROM public.rag_video_jobs WHERE id=%s", (ids[0],)
            ).fetchone()[0]
            == 0
        )


def test_web_actions_share_the_isolated_live_queue(isolated, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rag_favorite.web_console import create_app, password_hash

    _, video, ids, monitor = isolated
    monkeypatch.setattr(monitor, "_ensure_worker", lambda: None)
    origin = "https://isolated.example.ts.net"
    settings = {
        "origin": origin,
        "owner_login": "test@example.com",
        "allowed_ips": {"100.64.0.1"},
        "self_ips": {"100.64.0.1"},
        "username": "admin",
        "salt": "aa" * 16,
        "password_hash": password_hash("isolated-test-password", "aa" * 16),
    }
    app = create_app(monitor, settings)
    headers = {
        "x-forwarded-for": "100.64.0.1",
        "x-forwarded-proto": "https",
        "tailscale-user-login": "test@example.com",
        "origin": origin,
    }
    with TestClient(
        app, base_url=origin, client=("127.0.0.1", 1), headers=headers
    ) as client:
        response = client.post(
            "/api/login",
            json={"username": "admin", "password": "isolated-test-password"},
        )
        assert response.status_code == 200
        client.headers["x-csrf-token"] = response.json()["csrf"]
        client.portal.call(app.state.refresh)
        state = client.get("/api/snapshot")
        assert state.status_code == 200
        assert state.json()["snapshot"]["total"] == 3
        assert "DO_NOT_EXPOSE" not in state.text

        def action(name, job_id=None):
            body = {"action": name, "request_id": str(uuid4())}
            if job_id:
                body["job_id"] = job_id
            return client.post("/api/action", json=body)

        assert action("stop").status_code == 200
        assert read_control(video).paused
        assert action("stop_job", ids[0]).status_code == 200
        assert ids[0] in read_control(video).paused_jobs
        assert action("start").status_code == 200
        assert not read_control(video).paused
        assert ids[0] in read_control(video).paused_jobs
        assert action("start_job", ids[0]).status_code == 200
        assert ids[0] not in read_control(video).paused_jobs
        assert action("start_job", ids[2]).status_code == 200
        assert client.get("/api/snapshot").json()["snapshot"]["counts"]["queued"] == 3
        assert action("stop_job", ids[3]).status_code == 409
