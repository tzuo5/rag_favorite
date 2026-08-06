from __future__ import annotations

import subprocess

from backend.ingestion import cli, observation


def test_service_observation_reports_counts_without_log_content(
    monkeypatch,
) -> None:
    now = 100_000
    entered = int((now - 25 * 3600) * 1_000_000)

    def run(command):
        if command[0] == "systemctl":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "ActiveState=active\n"
                    f"ActiveEnterTimestampMonotonic={entered}\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "ordinary line\n"
                "ERROR request xsec_token=[REDACTED]\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(observation, "_run", run)
    monkeypatch.setattr(
        observation.time,
        "clock_gettime",
        lambda _clock: now,
    )

    result = observation.collect_service_observation(24)

    assert result["ok"] is True
    assert result["observation_window_complete"] is True
    assert result["raw_secret_hit_count"] == 0
    assert "ordinary line" not in repr(result)
    assert all(
        item["journal"]["error_line_count"] == 1
        for item in result["services"].values()
    )


def test_service_observation_detects_raw_secret_without_returning_it(
    monkeypatch,
) -> None:
    def run(command):
        if command[0] == "systemctl":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "ActiveState=active\n"
                    "ActiveEnterTimestampMonotonic=1\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="xsec_token=must-not-be-returned\n",
            stderr="",
        )

    monkeypatch.setattr(observation, "_run", run)
    monkeypatch.setattr(
        observation.time,
        "clock_gettime",
        lambda _clock: 100_000,
    )

    result = observation.collect_service_observation(1)

    assert result["ok"] is False
    assert result["raw_secret_hit_count"] == 4
    assert "must-not-be-returned" not in repr(result)


def test_persistence_security_audit_returns_only_counts() -> None:
    class Connection:
        @staticmethod
        def execute(_query, _params):
            return type("Result", (), {
                "fetchone": staticmethod(lambda: {"count": 0}),
            })()

    class Context:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_args):
            return None

    class Repository:
        @staticmethod
        def connection():
            return Context()

    result = cli.sensitive_persistence_audit(Repository())

    assert result["ok"] is True
    assert result["match_count"] == 0
    assert set(result["tables"]) == {
        "video_ingestion_jobs",
        "video_knowledge_documents",
        "video_author_discoveries",
        "video_author_discovery_items",
        "video_ingestion_batches",
        "video_ingestion_batch_items",
    }
