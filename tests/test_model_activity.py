import json
import os
from contextlib import nullcontext
from uuid import uuid4

import pytest

from rag_favorite.config import default_config
from rag_favorite.model_activity import (
    activity_call,
    cache_hit,
    job_activity,
    model_call,
    read_activity,
    read_calls,
)
from rag_favorite.video_config import VideoConfig


@pytest.fixture
def video(tmp_path):
    return VideoConfig(
        root=tmp_path / "owned", credentials_file=tmp_path / "secret.env"
    )


def test_real_activity_success_failure_cache_and_privacy(video):
    job = str(uuid4())
    with job_activity(video, job):
        with activity_call("ASR", "transcribe"):
            entry = read_activity(video)["ASR"]
            assert entry["status"] == "running" and entry["job_id"] == job
            assert entry["calls"] == 1 and entry["pid"] == os.getpid()
        entry = read_activity(video)["ASR"]
        assert entry["status"] == "success" and entry["completed_calls"] == 1
        assert entry["last_duration_seconds"] >= 0
        cache_hit("ASR", "transcript_chunk")
        assert read_activity(video)["ASR"]["cache_hits"] == 1
        assert read_activity(video)["ASR"]["calls"] == 1
        with (
            pytest.raises(RuntimeError, match="NEVER_EXPOSE"),
            activity_call("Embedding", "documents"),
        ):
            raise RuntimeError("NEVER_EXPOSE payload and credentials")
        assert read_activity(video)["Embedding"]["status"] == "failure"
    assert "NEVER_EXPOSE" not in (video.root / "model-activity.json").read_text()
    assert (video.root / "model-activity.json").stat().st_mode & 0o777 == 0o600


def test_monitoring_failure_does_not_fail_model_call(video):
    video.root.mkdir()
    (video.root / "model-activity.lock").mkdir()
    with job_activity(video, uuid4()), activity_call("CCR", "extract"):
        result = 42
    assert result == 42


def test_no_job_context_does_not_create_telemetry(video):
    @model_call("ImageBind", "embed-video")
    def encode(secret):
        return secret

    assert encode("fixture") == "fixture"
    assert not video.root.exists()


def test_corrupt_telemetry_is_advisory(video):
    video.root.mkdir()
    path = video.root / "model-activity.json"
    for content in [
        "{",
        "null",
        json.dumps(
            {"version": 1, "models": {"ASR": {"status": "running", "pid": "invalid"}}}
        ),
    ]:
        path.write_text(content)
        assert read_activity(video) == {}


def test_model_card_keeps_short_call_visible_and_separates_cache(video, monkeypatch):
    from rag_favorite import queue_monitor

    monkeypatch.setenv("RAG_ENCODER_TOKEN", "fixture")
    monkeypatch.setenv("RAG_CCR_API_KEY", "fixture")
    from dataclasses import replace

    asr = video.root / "asr-model"
    asr.mkdir(parents=True)
    video = replace(video, asr_model=asr)
    monkeypatch.setattr(
        queue_monitor.socket, "create_connection", lambda *a, **k: nullcontext()
    )
    monkeypatch.setattr(
        queue_monitor, "_embedding_health", lambda *a: (True, "模型已加载")
    )
    monkeypatch.setattr(
        queue_monitor,
        "read_router_status",
        lambda *a: {
            "name": "fixture",
            "live": True,
            "source": "test",
            "observed_at": None,
        },
    )
    worker = {
        "ActiveState": "active",
        "SubState": "running",
        "MainPID": str(os.getpid()),
    }
    job = {"id": str(uuid4()), "state": "running", "model": "ASR", "title": "fixture"}
    with job_activity(video, job["id"]):
        with activity_call("ASR", "transcribe"):
            cards = queue_monitor._models(default_config(), video, [job], worker)
            assert next(c for c in cards if c["key"] == "ASR")["state"] == "处理中"
        card = next(
            c
            for c in queue_monitor._models(default_config(), video, [], worker)
            if c["key"] == "ASR"
        )
        assert card["state"] == "刚完成" and "最近调用" in card["activity"]
        cache_hit("ASR", "transcript_chunk")
        card = next(
            c
            for c in queue_monitor._models(default_config(), video, [], worker)
            if c["key"] == "ASR"
        )
        assert (
            card["state"] == "缓存复用"
            and "调用 1 次 · 缓存复用 1 次" in card["activity"]
        )


def test_old_worker_record_cannot_claim_a_model_is_busy(video, monkeypatch):
    from rag_favorite import queue_monitor

    monkeypatch.setattr(
        queue_monitor.socket, "create_connection", lambda *a, **k: nullcontext()
    )
    monkeypatch.setattr(
        queue_monitor, "_embedding_health", lambda *a: (True, "模型已加载")
    )
    monkeypatch.setattr(
        queue_monitor,
        "read_router_status",
        lambda *a: {
            "name": "fixture",
            "live": True,
            "source": "test",
            "observed_at": None,
        },
    )
    with job_activity(video, uuid4()), activity_call("Embedding", "documents"):
        worker = {
            "ActiveState": "active",
            "SubState": "running",
            "MainPID": str(os.getpid() + 10000),
        }
        cards = queue_monitor._models(default_config(), video, [], worker)
        assert next(c for c in cards if c["key"] == "Embedding")["state"] == "空闲"


def test_overlapping_calls_remain_individually_visible(video):
    first, second = str(uuid4()), str(uuid4())
    with job_activity(video, first), activity_call("CCR", "extract"):
        with job_activity(video, second), activity_call("CCR", "summary"):
            assert len(read_calls(video)) == 2
        entry = read_activity(video)["CCR"]
        assert entry["status"] == "running" and entry["job_id"] == first
        assert entry["completed_calls"] == 1
    calls = read_calls(video)
    assert {c["job_id"] for c in calls.values()} == {first, second}
    assert all(c["status"] == "success" for c in calls.values())
    assert read_activity(video)["CCR"]["completed_calls"] == 2


def test_diagnostics_are_independent_of_call_success_and_allowlisted(video):
    from rag_favorite.model_activity import annotate_call

    with job_activity(video, uuid4()), activity_call("CCR", "extract"):
        annotate_call(
            http_status=200,
            stream_complete=True,
            valid_output=False,
            secret="NEVER_EXPOSE",
        )
    entry = next(iter(read_calls(video).values()))
    assert entry["status"] == "success" and entry["valid_output"] is False
    assert entry["http_status"] == 200 and entry["stream_complete"] is True
    assert "NEVER_EXPOSE" not in (video.root / "model-activity.json").read_text()
