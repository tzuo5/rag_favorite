from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from rag_favorite import video_worker
from rag_favorite.config import default_config
from rag_favorite.paths import AppPaths
from rag_favorite.video_config import VideoConfig, load_video_config
from rag_favorite.video_provider import CCRProvider, ProviderUnavailable
from rag_favorite.video_store import stage_asset
from rag_favorite.video_visual import (
    analyse_frame_batches,
    dense_provider_config,
    embed_complete_text,
    frame_times,
    request_frame_batch,
    validate_frame_summaries,
    visual_policy,
)


def profile(tmp_path):
    return VideoConfig(
        root=tmp_path / "owned",
        credentials_file=tmp_path / "secret",
        visual_enabled=True,
        asr_model=tmp_path / "model",
    )


@pytest.mark.parametrize(
    "source,interval,count",
    [
        ("no_speech_visual_only", 0.5, 1200),
        ("local_asr", 1.0, 600),
        ("supplied_subtitle", 1.0, 600),
    ],
)
def test_sampling_policy_includes_600_seconds_and_all_planned_frames(
    tmp_path, source, interval, count
):
    policy = visual_policy(profile(tmp_path), 600, source)
    assert policy["enabled"] and policy["frame_interval_seconds"] == interval
    assert policy["planned_frame_count"] == count
    assert not visual_policy(profile(tmp_path), 600.01, source)["enabled"]
    assert (
        visual_policy(profile(tmp_path), 601, source)["reason"]
        == "duration_exceeds_visual_limit"
    )


def test_sampling_grid_has_no_missing_boundary_frame_or_duplicate_endpoint():
    assert frame_times(0, 2, 0.5) == [0, 0.5, 1, 1.5]
    assert frame_times(0, 2, 1) == [0, 1]
    whole = frame_times(0, 60.25, 0.5)
    split = (
        frame_times(0, 30, 0.5) + frame_times(30, 60, 0.5) + frame_times(60, 60.25, 0.5)
    )
    assert whole == split and len(whole) == len(set(whole))
    with pytest.raises(ValueError):
        frame_times(0, float("inf"), 1)


def test_dense_quotas_allow_complete_analysis_beyond_old_hundred_image_limit(tmp_path):
    video = profile(tmp_path)
    policy = visual_policy(video, 600, "no_speech_visual_only")
    segments = [{"start": n, "end": n + 60} for n in range(0, 600, 60)]
    configured = dense_provider_config(video, policy, segments)
    provider = CCRProvider(configured)
    provider.start_visual_budget()
    for _ in range(150):
        provider.check_budget(8)
        provider.visual_requests += 1
        provider.visual_images += 8
    assert provider.visual_images == 1200 and provider.visual_requests == 150
    provider.visual_images = configured.visual_image_budget
    with pytest.raises(ProviderUnavailable, match="REQUEST_BUDGET"):
        provider.check_budget(1)
    assert configured.space_id == video.space_id


@pytest.mark.parametrize(
    "summaries",
    [
        [],
        [{"frame_index": 0, "caption": "画面"}],
        [{"frame_index": 0, "caption": "画面"}, {"frame_index": 0, "caption": "重复"}],
        [{"frame_index": 0, "caption": "画面"}, {"frame_index": 1, "caption": ""}],
    ],
)
def test_missing_duplicate_or_empty_frame_summaries_cannot_publish(summaries):
    with pytest.raises(ProviderUnavailable) as exc:
        validate_frame_summaries({"frame_summaries": summaries}, [0, 0.5])
    assert exc.value.code == "VISUAL_FRAME_COVERAGE_INCOMPLETE"


def test_each_batch_is_checkpointed_and_resume_does_not_repeat_completed_calls(
    tmp_path,
):
    video = profile(tmp_path)
    segment = {
        "ordinal": 0,
        "start": 0,
        "end": 5,
        "transcript": "",
        "frame_interval_seconds": 0.5,
    }
    times = frame_times(0, 5, 0.5)
    work, derived = video.root / "work", video.root / "derived"
    calls = []

    def ffmpeg(config, args):
        count = int(args[args.index("-frames:v") + 1])
        pattern = args[-1]
        for n in range(count):
            __import__("pathlib").Path(pattern % n).write_bytes(b"frame")

    def response(instruction, context, images):
        calls.append(len(images))
        if len(calls) == 2:
            raise ProviderUnavailable("temporary failure", code="CCR_REQUEST_FAILED")
        return {
            "caption": "场景",
            "facts": [
                {
                    "statement": "锅中翻炒",
                    "quote": "锅中翻炒",
                    "frame_index": len(images) - 1,
                    "evidence_kind": "visual",
                    "uncertain": False,
                }
            ],
            "edges": [],
            "frame_summaries": [
                {"frame_index": n, "caption": f"画面 {n}"} for n in range(len(images))
            ],
        }

    provider = SimpleNamespace(json=response)
    kwargs = {
        "ffmpeg": ffmpeg,
        "atomic_json": video_worker.atomic_json,
        "validate_extraction": video_worker.validate_extraction,
    }
    with pytest.raises(ProviderUnavailable):
        analyse_frame_batches(
            video,
            provider,
            tmp_path / "source",
            work,
            derived,
            segment,
            times,
            "key",
            "extract",
            **kwargs,
        )
    checkpoint = derived / "frame-batches" / "0-0.json"
    assert checkpoint.is_file()
    result, images = analyse_frame_batches(
        video,
        provider,
        tmp_path / "source",
        work,
        derived,
        segment,
        times,
        "key",
        "extract",
        **kwargs,
    )
    assert calls == [8, 2, 2] and len(images) == 10
    assert segment["analysed_frame_count"] == 10
    assert [s["seconds"] for s in segment["frame_summaries"]] == times
    assert "4.5s" in result["caption"]
    assert [f["frame_index"] for f in result["facts"]] == [7, 9]
    assert [f["frame_seconds"] for f in result["facts"]] == [3.5, 4.5]
    assert result["facts"][1]["observed_interval"] == [4.0, 5]


def test_long_dense_knowledge_is_fully_encoded_with_local_chunks():
    texts = []

    def encode(text):
        texts.append(text)
        return [1.0, 0.0]

    value = "完整视觉知识" * 2000
    result = embed_complete_text(SimpleNamespace(embed_document=encode), value)
    assert "".join(texts) == value and all(len(t) <= 3000 for t in texts)
    assert result == [1.0, 0.0]


@pytest.mark.parametrize("byte_limit", [4080, 2032, 7])
def test_dense_unicode_chunks_obey_loaded_context_bytes_without_losing_content(
    byte_limit,
):
    texts = []

    def encode(text):
        assert 0 < len(text.encode("utf-8")) <= byte_limit
        texts.append(text)
        return [1.0, 0.0]

    encoder = SimpleNamespace(
        embed_document=encode, document_byte_limit=lambda: byte_limit
    )
    value = "中英文😀标点。ASCII\n" * 700
    assert embed_complete_text(encoder, value) == [1.0, 0.0]
    assert "".join(texts) == value
    assert all(len(text) <= 3000 for text in texts)


def test_successful_short_embedding_input_is_not_repartitioned():
    texts = []
    encoder = SimpleNamespace(
        embed_document=lambda text: texts.append(text) or [1.0, 0.0],
        document_byte_limit=lambda: 4080,
    )
    value = "中" * 1300
    assert embed_complete_text(encoder, value) == [1.0, 0.0]
    assert texts == [value]


@pytest.mark.parametrize(
    "error",
    [
        "VISUAL_FRAME_COVERAGE_INCOMPLETE",
        "CCR_RESPONSE_TRUNCATED",
        "CCR_RESPONSE_INCOMPLETE",
        "CCR_RESPONSE_INVALID_JSON",
    ],
)
def test_incomplete_frame_batch_retries_smaller_groups_with_bounded_calls(error):
    calls = []

    def response(instruction, context, images):
        calls.append(len(images))
        if len(calls) == 1:
            raise ProviderUnavailable("invalid first batch", code=error)
        return {
            "frame_summaries": [
                {"frame_index": i, "caption": "完整帧"} for i in range(len(images))
            ],
            "facts": [],
            "edges": [],
            "caption": "场景",
        }

    parts = request_frame_batch(
        SimpleNamespace(json=response), "extract", "", list(range(8)), list(range(8))
    )
    assert calls == [8, 4, 4]
    assert [(p["offset"], p["count"]) for p in parts] == [(0, 4), (4, 4)]


def test_smaller_frame_batch_failure_is_not_retried_forever():
    calls = []

    def response(*args):
        calls.append(1)
        raise ProviderUnavailable("still truncated", code="CCR_RESPONSE_TRUNCATED")

    with pytest.raises(ProviderUnavailable):
        request_frame_batch(
            SimpleNamespace(json=response),
            "extract",
            "",
            list(range(8)),
            list(range(8)),
        )
    assert len(calls) == 2


def test_adaptive_parts_keep_all_frames_and_facts_and_resume_without_model_calls(
    tmp_path,
):
    video = profile(tmp_path)
    times = frame_times(0, 5, 0.5)
    segment = {
        "ordinal": 0,
        "start": 0,
        "end": 5,
        "transcript": "",
        "frame_interval_seconds": 0.5,
    }
    work, derived = video.root / "work", video.root / "derived"
    calls = []

    def ffmpeg(config, args):
        count = int(args[args.index("-frames:v") + 1])
        for i in range(count):
            __import__("pathlib").Path(args[-1] % i).write_bytes(b"frame")

    def response(instruction, context, images):
        calls.append(len(images))
        if len(images) == 8:
            return {"frame_summaries": []}
        return {
            "caption": "场景",
            "edges": [],
            "frame_summaries": [
                {"frame_index": i, "caption": "完整帧"} for i in range(len(images))
            ],
            "facts": [
                {
                    "statement": "画面操作",
                    "quote": "画面操作",
                    "evidence_kind": "visual",
                    "frame_index": i,
                    "uncertain": False,
                }
                for i in range(len(images))
            ],
        }

    kwargs = {
        "ffmpeg": ffmpeg,
        "atomic_json": video_worker.atomic_json,
        "validate_extraction": video_worker.validate_extraction,
    }
    args = (
        video,
        SimpleNamespace(json=response),
        tmp_path / "source",
        work,
        derived,
        segment,
        times,
        "key",
        "extract",
    )
    result, _ = analyse_frame_batches(*args, **kwargs)
    assert calls == [8, 4, 4, 2]
    assert [f["frame_index"] for f in result["facts"]] == list(range(10))
    assert [f["frame_seconds"] for f in result["facts"]] == times
    assert [s["seconds"] for s in segment["frame_summaries"]] == times
    assert segment["visual_batches"][0]["caption"] == "场景\n场景"
    result2, _ = analyse_frame_batches(*args, **kwargs)
    assert calls == [8, 4, 4, 2] and result2 == result


@pytest.mark.parametrize("has_asr_model", [True, False])
def test_no_audio_track_does_not_require_fake_audio_extraction(
    tmp_path, monkeypatch, has_asr_model
):
    video = profile(tmp_path)
    if not has_asr_model:
        video = replace(video, asr_model=None)
    monkeypatch.setattr(
        video_worker, "local_encoder", lambda *args: {"duration": 2, "has_audio": False}
    )
    monkeypatch.setattr(
        video_worker,
        "ffmpeg",
        lambda *args: pytest.fail("Audio extraction should not run"),
    )
    transcript, source = video_worker.prepare_transcript(
        video, {"sha256": "source"}, tmp_path / "source", video.root / "work"
    )
    assert transcript == [] and source == "no_speech_visual_only"


def test_audio_track_still_requires_asr_model_without_supplied_subtitles(
    tmp_path, monkeypatch
):
    video = replace(profile(tmp_path), asr_model=None)
    monkeypatch.setattr(
        video_worker, "local_encoder", lambda *a: {"duration": 2, "has_audio": True}
    )
    with pytest.raises(ProviderUnavailable) as error:
        video_worker.prepare_transcript(
            video, {"sha256": "source"}, tmp_path / "source", video.root / "work"
        )
    assert error.value.code == "TRANSCRIPT_REQUIRED"


@pytest.mark.parametrize("duration,no_speech", [(601, False), (601, True)])
def test_long_video_never_enters_visual_decode_or_embedding_and_still_writes_markdown(
    tmp_path, monkeypatch, duration, no_speech
):
    video = profile(tmp_path)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"owned video")
    staged = stage_asset(source, video)
    job_id = str(uuid4())
    job = {
        "id": job_id,
        "state": "running",
        "collection": "cooking",
        "payload": {
            "asset_id": staged["asset_id"],
            "sha256": staged["sha256"],
            "source_label": "owner source",
            "title": "长视频",
        },
    }
    transcript = (
        []
        if no_speech
        else [
            {
                "start": 0,
                "end": 1,
                "text": "已有语音知识",
                "timing_precision": "segment",
            }
        ]
    )
    video_worker.atomic_json(
        video.root / "derived" / job_id / "source.json",
        {"description": "作者提供的文字知识"},
    )
    monkeypatch.setattr(
        video_worker,
        "prepare_transcript",
        lambda *a, **k: (
            transcript,
            "no_speech_visual_only" if no_speech else "local_asr",
        ),
    )

    def encoder(config, operation, payload):
        assert operation == "probe"
        return {"duration": duration}

    monkeypatch.setattr(video_worker, "local_encoder", encoder)
    monkeypatch.setattr(
        video_worker,
        "ffmpeg",
        lambda *a: pytest.fail("Visual decoding attempted above 600 seconds"),
    )
    config = default_config(AppPaths.discover())
    monkeypatch.setattr(video_worker, "resolve_index", lambda c: c)
    monkeypatch.setattr(
        video_worker,
        "summarize_job",
        lambda *a, **k: {"state": "complete"},
    )

    def response(instruction, text, images=None):
        assert not images and text.strip()
        return {"caption": "文字摘要", "facts": [], "edges": []}

    monkeypatch.setattr(
        video_worker,
        "CCRProvider",
        lambda *a, **k: SimpleNamespace(
            json=response, calls=[], check_budget=lambda *a: None
        ),
    )
    monkeypatch.setattr(video_worker, "update_job", lambda *a: None)
    published = []
    monkeypatch.setattr(video_worker, "publish", lambda *a: published.append(a[-2]))
    monkeypatch.setattr(video_worker, "cleanup", lambda *a: None)
    video_worker.process(config, video, job)
    record = published[0]
    assert not record["visual_executed"] and record["analysed_frame_count"] == 0
    assert all(s["text_embedding"] is None for s in record["segments"])
    assert record["visual_policy"]["reason"] == "duration_exceeds_visual_limit"
    markdown = (video.root / "derived" / job_id / "knowledge.md").read_text()
    assert "视觉分析已跳过" in markdown and "owner source" in markdown
    if no_speech:
        assert record["transcript_source"] == "author_note_no_speech"
        assert record["segments"][0]["start"] is None


def test_new_configuration_records_requested_sampling_policy(tmp_path):
    path = tmp_path / "video.toml"
    path.write_text("")
    config = load_video_config(path)
    assert (
        config.silent_frame_interval_seconds,
        config.speech_frame_interval_seconds,
        config.visual_max_duration_seconds,
    ) == (0.5, 1.0, 600)
    assert config.visual_budget_seconds == 0
    assert (
        config.space_id == replace(config, silent_frame_interval_seconds=1.0).space_id
    )


def test_silent_long_video_without_any_text_blocks_with_markdown_explanation(
    tmp_path, monkeypatch
):
    video = profile(tmp_path)
    source = tmp_path / "silent.mp4"
    source.write_bytes(b"owned video")
    staged = stage_asset(source, video)
    job_id = str(uuid4())
    job = {
        "id": job_id,
        "state": "running",
        "collection": "cooking",
        "payload": {
            "asset_id": staged["asset_id"],
            "sha256": staged["sha256"],
            "title": "长静音视频",
        },
    }
    monkeypatch.setattr(video_worker, "local_encoder", lambda *a: {"duration": 601})
    monkeypatch.setattr(
        video_worker,
        "prepare_transcript",
        lambda *a, **k: ([], "no_speech_visual_only"),
    )
    monkeypatch.setattr(video_worker, "update_job", lambda *a: None)
    monkeypatch.setattr(
        video_worker,
        "resolve_index",
        lambda *a: pytest.fail("An empty long video must not reach model extraction"),
    )
    with pytest.raises(ProviderUnavailable) as error:
        video_worker.process(default_config(AppPaths.discover()), video, job)
    assert error.value.code == "NO_SPEECH_VISUAL_SKIPPED_NO_TEXT"
    markdown = (video.root / "derived" / job_id / "knowledge.md").read_text()
    assert "没有从视频提取知识" in markdown and "601" in markdown
    assert (video.root / "assets" / staged["asset_id"]).exists()
