import json
import threading
import time
from uuid import uuid4

import pytest

from rag_favorite.config import ConfigError
from rag_favorite.queue_control import locked_control, read_control, set_control
from rag_favorite.queue_monitor import describe_stage, worker_supports_control
from rag_favorite.video_config import VideoConfig


@pytest.fixture
def video(tmp_path):
    return VideoConfig(
        root=tmp_path / "owned", credentials_file=tmp_path / "secret.env"
    )


def test_absent_gate_is_read_only_and_defaults_to_running(video):
    assert not read_control(video).paused
    assert not video.root.exists()


def test_pause_survives_reload_and_global_start_preserves_individual_pause(video):
    job_id = str(uuid4())
    set_control(video, job_id=job_id, job_paused=True)
    set_control(video, paused=True)
    assert read_control(video).paused
    restored = set_control(video, paused=False)
    assert not restored.paused and restored.paused_jobs == (job_id,)
    set_control(video, job_id=job_id, job_paused=False)
    assert read_control(video).paused_jobs == ()
    assert (video.root / "queue-control.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "{}",
        '{"version":1,"paused":"false","paused_jobs":[]}',
        '{"version":1,"paused":false,"paused_jobs":["bad"]}',
    ],
)
def test_malformed_gate_never_silently_unpauses(video, content):
    video.root.mkdir()
    (video.root / "queue-control.json").write_text(content)
    with pytest.raises(ConfigError, match="stays paused"):
        read_control(video)
    with pytest.raises(ConfigError):
        set_control(video, paused=False)


def test_stop_waits_for_claim_critical_section(video):
    entered = threading.Event()
    finished = threading.Event()
    errors = []

    def stop():
        entered.set()
        try:
            set_control(video, paused=True)
        except (ConfigError, OSError, ValueError) as exc:
            errors.append(exc)
        finally:
            finished.set()

    with locked_control(video):
        thread = threading.Thread(target=stop)
        thread.start()
        assert entered.wait(2)
        assert not finished.wait(0.1)
        assert not read_control(video).paused
    thread.join(2)
    assert not errors and finished.is_set() and read_control(video).paused


def test_control_lock_timeout_does_not_change_queue_state(video):
    with locked_control(video):
        began = time.monotonic()
        with pytest.raises(ConfigError, match="本次操作未完成"):
            set_control(video, paused=True, lock_timeout=0.1)
        assert time.monotonic() - began < 0.5
        assert not read_control(video).paused


def test_model_and_local_progress_have_no_fabricated_total_percentage():
    assert describe_stage("extract:2:frames:8/30") == (
        "视觉分析",
        "CCR",
        "段 3 · 8/30 帧",
    )
    assert describe_stage("transcript:1/4") == ("音频转写", "ASR", "1/4 音频片段")
    assert describe_stage("embed_video:0")[1] == "ImageBind"
    assert describe_stage("embed_text:0")[1] == "Embedding"
    assert describe_stage("review:0")[1] == "CCR"
    assert describe_stage("failed:transcript")[0] == "失败 · 音频转写"
    assert describe_stage("fetch_source")[2] == "—"


def test_capability_belongs_to_current_worker_pid(video):
    video.root.mkdir()
    (video.root / "queue-worker.json").write_text(
        json.dumps({"pid": 123, "queue_control_version": 1})
    )
    assert worker_supports_control({"MainPID": "123"}, video)
    assert not worker_supports_control({"MainPID": "456"}, video)
    assert not worker_supports_control({"MainPID": "0"}, video)


@pytest.mark.parametrize("loaded,expected", [(True, True), (False, False)])
def test_embedding_status_distinguishes_loaded_model_from_open_port(
    monkeypatch, loaded, expected
):
    from dataclasses import replace
    from io import BytesIO

    from rag_favorite.config import default_config
    from rag_favorite.queue_monitor import _embedding_health

    config = default_config()
    config = replace(
        config,
        embedding=replace(
            config.embedding,
            backend="lmstudio",
            url="http://127.0.0.1:1234/v1/embeddings",
            model="expected-model",
        ),
    )
    inventory = {
        "models": [
            {
                "type": "embedding",
                "loaded_instances": [{"id": "expected-model"}] if loaded else [],
            }
        ]
    }
    monkeypatch.setattr(
        "rag_favorite.queue_monitor.urllib.request.urlopen",
        lambda *args, **kwargs: BytesIO(json.dumps(inventory).encode()),
    )
    state, description = _embedding_health(config)
    assert state is expected
    assert ("模型已加载" if loaded else "模型未加载") in description
