"""Offline stage integration: real artifacts, no model, database or HTTP calls."""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from rag_favorite import title_dedup, video_worker, xhs_images
from rag_favorite.config import ConfigError, default_config
from rag_favorite.video_config import VideoConfig
from rag_favorite.video_store import digest_file, stage_asset


@pytest.fixture
def stages(tmp_path, monkeypatch):
    video = VideoConfig(
        root=tmp_path / "owned",
        credentials_file=tmp_path / "unused",
        visual_enabled=True,
    )
    config = default_config()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source fixture")
    manifest = stage_asset(source, video)
    job = {
        "id": str(uuid4()),
        "state": "running",
        "collection": "cooking",
        "payload": {**manifest, "title": "offline fixture"},
    }
    events = []
    monkeypatch.setattr(title_dedup, "deduplicate_job", lambda *_: False)
    monkeypatch.setattr(video_worker, "update_job", lambda *_: None)
    monkeypatch.setattr(video_worker, "resolve_index", lambda _: config)
    monkeypatch.setattr(video_worker, "publish", lambda *_: events.append("publish"))
    monkeypatch.setattr(video_worker, "cleanup", lambda *_: events.append("cleanup"))

    def encoder(_video, operation, payload):
        events.append(operation)
        if operation == "probe":
            return {"duration": 2}
        if operation == "embed-video":
            return {"vector": [1.0] + [0.0] * 1023, "space_id": video.space_id}
        pytest.fail("Unexpected local operation " + operation)

    def transcript(*_args, **_kwargs):
        events.append("asr")
        return [
            {
                "start": 0.0,
                "end": 2.0,
                "text": "原始证据",
                "timing_precision": "segment",
            }
        ], "local_asr"

    def ffmpeg(_video, args):
        events.append("ffmpeg")
        output = Path(args[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        if "%06d" in output.name:
            count = int(args[args.index("-frames:v") + 1])
            for index in range(count):
                Path(str(output).replace("%06d", f"{index:06d}")).write_bytes(
                    f"frame-{index}".encode()
                )
        else:
            output.write_bytes(b"clip")

    class Provider:
        def __init__(self, *_args, **_kwargs):
            self.calls = []

        def check_budget(self, *_):
            pass

        def start_visual_budget(self):
            pass

        def json(self, instruction, text, images=None):
            events.append("ccr")
            self.calls.append({"completed": True})
            result = {"caption": "证据画面", "facts": [], "edges": []}
            if images:
                result["frame_summaries"] = [
                    {"frame_index": i, "caption": "画面"} for i in range(len(images))
                ]
            return result

    def summary(*_args, phase="all", **_kwargs):
        events.append("summary:" + phase)
        return {"state": "generated" if phase == "generate" else "complete"}

    monkeypatch.setattr(video_worker, "local_encoder", encoder)
    monkeypatch.setattr(video_worker, "prepare_transcript", transcript)
    monkeypatch.setattr(video_worker, "ffmpeg", ffmpeg)
    monkeypatch.setattr(video_worker, "CCRProvider", Provider)
    monkeypatch.setattr(video_worker, "summarize_job", summary)
    return SimpleNamespace(video=video, config=config, job=job, events=events)


def test_four_video_stages_separate_local_and_llm_work(stages):
    video_worker.process(stages.config, stages.video, stages.job, phase="download")
    assert stages.events == []
    video_worker.process(stages.config, stages.video, stages.job, phase="prepare")
    assert "asr" in stages.events and "embed-video" in stages.events
    assert "ccr" not in stages.events and not any(
        e.startswith("summary") for e in stages.events
    )
    directory = stages.video.root / "derived" / stages.job["id"]
    assert (directory / "prepared.json").is_file()
    assert not (directory / "record.json").exists()
    stages.events.clear()
    video_worker.process(stages.config, stages.video, stages.job, phase="llm")
    assert stages.events == ["ccr", "summary:generate"]
    record = json.loads((directory / "record.json").read_text())
    assert record["analysed_frame_count"] == 2
    assert record["segments"][0]["video_embedding"][0] == 1
    assert (directory / "knowledge.md").is_file()
    stages.events.clear()
    video_worker.process(stages.config, stages.video, stages.job, phase="publish")
    assert stages.events == ["summary:publish", "publish", "cleanup"]


@pytest.mark.parametrize("change", ["frame", "source", "profile"])
def test_llm_refuses_changed_preparation_before_model_call(stages, change):
    video_worker.process(stages.config, stages.video, stages.job, phase="prepare")
    path = stages.video.root / "derived" / stages.job["id"] / "prepared.json"
    prepared = json.loads(path.read_text())
    if change == "frame":
        frame = prepared["context"]["segments"][0]["prepared_frames"][0]
        (stages.video.root / frame["path"]).write_bytes(b"changed")
    elif change == "source":
        prepared["source_sha256"] = "changed"
        path.write_text(json.dumps(prepared))
    else:
        prepared["profile"] = "changed"
        path.write_text(json.dumps(prepared))
    stages.events.clear()
    with pytest.raises(ConfigError, match="PIPELINE_PREPAR"):
        video_worker.process(stages.config, stages.video, stages.job, phase="llm")
    assert "ccr" not in stages.events and "asr" not in stages.events


def test_draft_resume_never_repeats_media_or_extract_calls(stages):
    video_worker.process(stages.config, stages.video, stages.job, phase="prepare")
    video_worker.process(stages.config, stages.video, stages.job, phase="llm")
    stages.events.clear()
    video_worker.process(stages.config, stages.video, stages.job, phase="prepare")
    video_worker.process(stages.config, stages.video, stages.job, phase="llm")
    assert stages.events == ["summary:generate"]


def test_missing_preparation_and_draft_are_explicit(stages):
    with pytest.raises(ConfigError, match="PIPELINE_PREPARATION_MISSING"):
        video_worker.process(stages.config, stages.video, stages.job, phase="llm")
    with pytest.raises(ConfigError, match="PIPELINE_DRAFT_MISSING"):
        video_worker.process(stages.config, stages.video, stages.job, phase="publish")
    assert stages.events == []


def test_prepared_frame_cannot_escape_owned_work_directory(stages):
    outside = stages.video.root / "assets" / "outside.jpg"
    outside.write_bytes(b"fixture")
    segment = {
        "prepared_frames": [
            {
                "path": str(outside.relative_to(stages.video.root)),
                "sha256": digest_file(outside),
            }
        ]
    }
    with pytest.raises(ConfigError, match="PIPELINE_PREPARED_FRAME_CHANGED"):
        video_worker.prepared_images(stages.video, segment)


def test_missing_prepared_frame_coverage_never_calls_model(stages):
    video_worker.process(stages.config, stages.video, stages.job, phase="prepare")
    path = stages.video.root / "derived" / stages.job["id"] / "prepared.json"
    prepared = json.loads(path.read_text())
    prepared["context"]["segments"][0]["prepared_frames"].pop()
    path.write_text(json.dumps(prepared))
    stages.events.clear()
    with pytest.raises(video_worker.ProviderUnavailable) as caught:
        video_worker.process(stages.config, stages.video, stages.job, phase="llm")
    assert caught.value.code == "VISUAL_FRAME_COVERAGE_INCOMPLETE"
    assert stages.events == []


def test_summary_generation_failure_retains_draft_and_never_publishes(
    stages, monkeypatch
):
    video_worker.process(stages.config, stages.video, stages.job, phase="prepare")

    def fail(*_a, **_k):
        raise video_worker.ProviderUnavailable("failed", code="CCR_REQUEST_FAILED")

    monkeypatch.setattr(video_worker, "summarize_job", fail)
    stages.events.clear()
    with pytest.raises(video_worker.ProviderUnavailable):
        video_worker.process(stages.config, stages.video, stages.job, phase="llm")
    directory = stages.video.root / "derived" / stages.job["id"]
    assert (directory / "record.json").is_file() and (
        directory / "knowledge.md"
    ).is_file()
    assert stages.events == ["ccr"]
    assert (stages.video.root / "work" / stages.job["id"]).exists()


@pytest.mark.parametrize("tamper", [None, "image", "ocr"])
def test_image_prepare_downloads_and_ocrs_then_llm_uses_cache(
    stages, monkeypatch, tmp_path, tamper
):
    from PIL import Image

    job = stages.job
    job["payload"]["source_url"] = "https://www.xiaohongshu.com/explore/" + "a" * 24
    note = {
        "note_id": "a" * 24,
        "title": "图片证据",
        "description": "正文",
        "images": [{"image_index": 0, "url": "https://example.invalid/image.jpg"}],
    }
    directory = stages.video.root / "derived" / job["id"]
    video_worker.atomic_json(directory / "image-source.json", note)
    config = SimpleNamespace(
        collection=lambda _: SimpleNamespace(path=tmp_path / "notes")
    )

    def download(_url, path, **_kwargs):
        stages.events.append("image-download")
        Image.new("RGB", (8, 8)).save(path)
        return {"sha256": digest_file(path)}

    def ocr(_path):
        stages.events.append("ocr")
        return [{"text": "图片文字"}]

    class ImageProvider:
        def __init__(self, *_a, **_k):
            pass

        def start_visual_budget(self):
            pass

        def json(self, *_a, **_k):
            stages.events.append("image-ccr")
            return {"caption": "图片内容", "facts": []}

    monkeypatch.setattr(xhs_images, "download_image", download)
    monkeypatch.setattr(xhs_images, "local_ocr", ocr)
    monkeypatch.setattr(xhs_images, "CCRProvider", ImageProvider)
    video_worker.process(config, stages.video, job, phase="download")
    assert stages.events == []
    video_worker.process(config, stages.video, job, phase="prepare")
    assert stages.events == ["image-download", "ocr"]
    stages.events.clear()
    if tamper:
        if tamper == "image":
            (directory / "images" / "0000.jpg").write_bytes(b"changed")
        else:
            image_manifest = directory / "image-note.json"
            prepared = json.loads(image_manifest.read_text())
            prepared["images"]["0"].pop("ocr")
            image_manifest.write_text(json.dumps(prepared))
        with pytest.raises(video_worker.ProviderUnavailable) as caught:
            video_worker.process(config, stages.video, job, phase="llm")
        assert caught.value.code == "XHS_IMAGE_NOTE_INCOMPLETE"
        assert stages.events == []
        return
    video_worker.process(config, stages.video, job, phase="llm")
    assert stages.events == ["image-ccr", "summary:generate"]
    stages.events.clear()
    video_worker.process(config, stages.video, job, phase="llm")
    assert stages.events == ["summary:generate"]


@pytest.mark.parametrize("tamper", ["missing", "dimension", "nonfinite", "space"])
def test_invalid_prepared_vector_refused_before_any_ccr(stages, tamper):
    video_worker.process(stages.config, stages.video, stages.job, phase="prepare")
    path = stages.video.root / "derived" / stages.job["id"] / "prepared.json"
    prepared = json.loads(path.read_text())
    segment = prepared["context"]["segments"][0]
    if tamper == "missing":
        segment.pop("video_embedding")
    elif tamper == "dimension":
        segment["video_embedding"] = [1.0]
    elif tamper == "nonfinite":
        segment["video_embedding"][0] = float("inf")
    else:
        segment["video_space_id"] = "changed"
    path.write_text(json.dumps(prepared))
    stages.events.clear()
    with pytest.raises(ConfigError, match="PIPELINE_PREPARED_VECTOR_INVALID"):
        video_worker.process(stages.config, stages.video, stages.job, phase="llm")
    assert stages.events == []
