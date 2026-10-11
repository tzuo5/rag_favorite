import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    "current_stamp,expected_resume", [("deployment", True), ("user_changed", False)]
)
def test_worker_refresh_waits_for_job_and_preserves_user_control(
    tmp_path, monkeypatch, current_stamp, expected_resume
):
    spec = importlib.util.spec_from_file_location(
        "worker_refresh", Path(__file__).parents[1] / "examples/refresh_video_worker.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    counts, events = iter([1, 0]), []

    class Database:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, *args):
            count = next(counts)
            return SimpleNamespace(fetchone=lambda: (count,))

    monkeypatch.setattr(module, "load_config", lambda: None)
    monkeypatch.setattr(
        module, "load_video_config", lambda: SimpleNamespace(root=tmp_path)
    )
    monkeypatch.setattr(module, "library_id", lambda v: "test")
    monkeypatch.setattr(module, "connect_database", lambda c: Database())
    monkeypatch.setattr(
        module, "read_control", lambda v: SimpleNamespace(updated_at=current_stamp)
    )
    monkeypatch.setattr(module, "set_control", lambda *a, **kw: events.append("resume"))
    monkeypatch.setattr(module.time, "sleep", lambda s: events.append("wait"))
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **kw: events.append("restart")
    )
    monkeypatch.setattr("sys.argv", ["refresh", "--pause-stamp", "deployment"])
    module.main()
    assert (
        events == ["wait", "restart", "resume"]
        if expected_resume
        else events == ["wait", "restart"]
    )
