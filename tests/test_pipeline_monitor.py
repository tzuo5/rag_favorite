"""Offline monitor checks distinguish transport, output and persisted publications."""

import json
from datetime import UTC, datetime, timedelta

from rag_favorite import queue_monitor
from rag_favorite.video_config import VideoConfig


def test_ccr_metrics_use_terminal_annotated_attempts_without_treating_http_as_output():
    now = datetime(2026, 10, 10, 12, tzinfo=UTC)
    base = {"model": "CCR", "status": "success", "completed_at": now.isoformat()}
    calls = [
        dict(base, http_status=200, stream_complete=True, valid_output=True),
        dict(base, http_status=200, stream_complete=False, valid_output=False),
        dict(base, status="failure", http_status=None, error_category="network"),
        dict(
            base,
            status="cached",
            http_status=200,
            stream_complete=True,
            valid_output=True,
        ),
        dict(base, status="running", http_status=200),
        dict(base, model="ASR", http_status=200),
        dict(base, completed_at=(now - timedelta(days=2)).isoformat(), http_status=200),
        dict(base),  # Legacy records and a pause before transmission are unknown.
        dict(base, completed_at="bad", http_status=200),
    ]
    metrics = queue_monitor._call_metrics(calls, now)
    assert metrics["attempts"] == 3
    assert metrics["http_success"] == 2
    assert metrics["valid_outputs"] == 1
    assert metrics["unknown_attempts"] == 1
    assert metrics["window_start"] == (now - timedelta(hours=24)).isoformat()


def test_call_projection_keeps_multiple_active_requests_and_excludes_payload(tmp_path):
    video = VideoConfig(root=tmp_path, credentials_file=tmp_path / "unused.env")
    call = {
        "model": "CCR",
        "job_id": "job",
        "pid": 123,
        "status": "running",
        "updated_at": datetime.now(UTC).isoformat(),
        "operation": "extract",
        "started_at": datetime.now(UTC).isoformat(),
    }
    (tmp_path / "model-activity.json").write_text(
        json.dumps(
            {
                "version": 1,
                "models": {},
                "calls": {
                    "a": dict(call, call_id="a", request_body="secret-input"),
                    "b": dict(call, call_id="b", api_key="secret-credential"),
                },
            }
        )
    )
    records = queue_monitor._call_records(video)
    assert {record["call_id"] for record in records} == {"a", "b"}
    assert "secret" not in json.dumps(records)


def test_unmigrated_search_index_is_unknown_instead_of_counting_complete_jobs(tmp_path):
    from rag_favorite.config import default_config

    class Database:
        def execute(self, query, parameters):
            assert "to_regclass" in query
            return self

        def fetchone(self):
            return (None, None, None, None)

    assert (
        queue_monitor._searchable_completed(
            Database(),
            default_config(),
            VideoConfig(root=tmp_path, credentials_file=tmp_path / "unused.env"),
        )
        is None
    )
