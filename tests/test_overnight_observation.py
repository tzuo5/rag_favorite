import importlib.util
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from rag_favorite.model_activity import activity_call, annotate_call, job_activity
from rag_favorite.observation import span, timed
from rag_favorite.video_config import VideoConfig

spec = importlib.util.spec_from_file_location(
    "overnight_monitor", Path(__file__).parents[1] / "scripts/overnight_monitor.py"
)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def enabled_video(tmp_path, deadline=None):
    video = VideoConfig(root=tmp_path / "video", credentials_file=tmp_path / "private")
    output = video.root / "observations" / "test-run"
    output.mkdir(parents=True)
    (video.root / "observation-control.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "run_id": "test-run",
                "ends_at_epoch": deadline or time.time() + 30,
            }
        )
    )
    return video, output


def test_durable_events_keep_overlapping_calls_and_do_not_serialize_arguments(tmp_path):
    video, output = enabled_video(tmp_path)

    @timed("fixture_decode")
    def operation(secret):
        return secret

    with (
        job_activity(video, uuid4()),
        span("stage", "prepare", token=3, attempt=2),
        activity_call("CCR", "extract"),
    ):
        annotate_call(http_status=200, valid_output=True, secret="DO_NOT_SAVE")
        with activity_call("ASR", "transcribe"):
            assert operation("DO_NOT_SAVE") == "DO_NOT_SAVE"
    events = monitor.rows(output / "events.jsonl")
    assert CounterFor(events, "model_start") == 2
    assert CounterFor(events, "model_end") == 2
    assert CounterFor(events, "span_start") == 2
    assert "DO_NOT_SAVE" not in (output / "events.jsonl").read_text()
    assert (output / "events.jsonl").stat().st_mode & 0o777 == 0o600
    assert len({e["call_id"] for e in events if "call_id" in e}) == 2


def CounterFor(events, event):
    return sum(e["event"] == event for e in events)


def test_timing_exception_is_preserved_and_deadline_disables_recording(tmp_path):
    video, output = enabled_video(tmp_path)
    with (
        pytest.raises(ValueError, match="PRIVATE_TEXT"),
        job_activity(video, uuid4()),
        span("operation", "fixture"),
    ):
        raise ValueError("PRIVATE_TEXT")
    terminal = monitor.rows(output / "events.jsonl")[-1]
    assert terminal["outcome"] == "failure" and terminal["error_code"] == "ValueError"
    assert "PRIVATE_TEXT" not in (output / "events.jsonl").read_text()
    expired, expired_output = enabled_video(tmp_path / "expired", time.time() - 1)
    with job_activity(expired, uuid4()), span("operation", "expired"):
        result = 7
    assert result == 7 and not (expired_output / "events.jsonl").exists()


def test_concurrent_event_appends_are_complete_and_context_local(tmp_path):
    video, output = enabled_video(tmp_path)
    ids = [str(uuid4()) for _ in range(30)]

    def write(job):
        with job_activity(video, job), span("stage", "llm"):
            pass

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, ids))
    events = monitor.rows(output / "events.jsonl")
    assert len(events) == 60
    assert {e["job_id"] for e in events} == set(ids)
    assert all(e["parent_span_id"] is None for e in events)


def test_observation_failure_never_changes_result(tmp_path):
    video, output = enabled_video(tmp_path)
    (output / "events.jsonl").mkdir()
    with job_activity(video, uuid4()), span("operation", "fixture"):
        result = 42
    assert result == 42
    assert list(video.root.glob("observation-errors-*.jsonl"))


def test_concurrency_uses_half_open_intervals_and_window_clipping():
    value = monitor.concurrency([(0, 10), (5, 15), (15, 20), (-5, -1)], 0, 20)
    assert value == {
        "peak": 2,
        "average_over_window": 1.25,
        "overlap_seconds": 5,
        "total_call_seconds": 25,
    }
    assert monitor.concurrency([(0, 5), (5, 10)], 0, 10)["peak"] == 1


def test_different_models_excludes_same_model_concurrency():
    same = monitor.different_models({"CCR": [(0, 10), (2, 8)]}, 0, 10)
    assert same["peak_distinct_models"] == 1 and same["overlap_seconds"] == 0
    mixed = monitor.different_models(
        {"CCR": [(0, 10), (2, 8)], "ASR": [(5, 15)]}, 0, 20
    )
    assert mixed["peak_distinct_models"] == 2
    assert mixed["overlap_seconds"] == 5
    assert mixed["pair_overlap_seconds"] == {"ASR+CCR": 5}


def test_report_excludes_historical_completions_and_deduplicates_call_ledger(tmp_path):
    now = datetime.now(UTC)
    beginning, finish = (now - timedelta(seconds=30)).isoformat(), now.isoformat()
    earlier = (now - timedelta(hours=1)).isoformat()
    old_id, new_id = str(uuid4()), str(uuid4())
    old = {
        "job_id": old_id,
        "state": "complete",
        "stage": "complete",
        "created_at": earlier,
        "updated_at": earlier,
        "elapsed_seconds": 0,
    }
    new = {
        "job_id": new_id,
        "state": "queued",
        "stage": "queued",
        "created_at": earlier,
        "updated_at": earlier,
    }
    monitor.save(
        tmp_path / "manifest.json",
        {"started_at": beginning, "ends_at": finish, "ccr_concurrency": 6},
    )
    monitor.save(tmp_path / "baseline.json", {"jobs": [old, new], "stages": []})
    monitor.save(
        tmp_path / "final.json",
        {
            "at": finish,
            "jobs": [old, dict(new, state="complete", updated_at=finish)],
            "stages": [],
            "searchable_job_ids": [old_id, new_id],
        },
    )
    call = {
        "call_id": "call1",
        "job_id": new_id,
        "model": "CCR",
        "operation": "extract",
        "started_at": beginning,
        "completed_at": finish,
        "duration_seconds": 30,
        "status": "success",
        "request_id": "request1",
    }
    monitor.append(tmp_path / "call-snapshots.jsonl", {"at": finish, "calls": [call]})
    monitor.append(
        tmp_path / "provider-ledgers.jsonl",
        {
            "job_id": new_id,
            "ledger": "provider-usage.jsonl",
            "source_offset": 0,
            "client_request_id": "request1",
            "request_id": "gateway-request1",
            "model": "requested",
            "response_model": "actual",
            "completed_at": finish,
            "created_at": beginning,
            "seconds": 30,
            "completed": True,
            "json_valid": True,
        },
    )
    summary = monitor.report(tmp_path)
    assert summary["new_complete_jobs"] == 1
    assert summary["new_complete_and_searchable_jobs"] == 1
    assert summary["model_call_outcomes"] == {"success": 1}
    assert summary["model_client_concurrency"]["CCR"]["peak"] == 1
    assert (tmp_path / "report.html").exists()
    assert "actual" in (tmp_path / "calls.csv").read_text()
    assert "gateway-request1" in (tmp_path / "calls.csv").read_text()
