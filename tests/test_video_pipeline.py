from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from rag_favorite.config import ConfigError, default_config
from rag_favorite.paths import AppPaths
from rag_favorite.video_config import VideoConfig
from rag_favorite.video_mcp import create_video_mcp
from rag_favorite.video_retrieval import reciprocal_rank_fusion
from rag_favorite.video_store import asset_path, stage_asset
from rag_favorite.video_transcript import parse_transcript, partition_transcript
from rag_favorite.video_worker import cleanup, merge_numeric_review, validate_extraction


def profile(tmp_path):
    return VideoConfig(
        root=tmp_path / "owned", credentials_file=tmp_path / "secret.env"
    )


def test_staging_never_consumes_external_source_and_rejects_tampering(tmp_path):
    config = profile(tmp_path)
    source = tmp_path / "external.mp4"
    source.write_bytes(b"video fixture")
    record = stage_asset(source, config)
    owned, _ = asset_path(config, record["asset_id"])
    assert source.read_bytes() == owned.read_bytes()
    owned.write_bytes(b"tampered")
    with pytest.raises(ConfigError, match="checksum"):
        asset_path(config, record["asset_id"])
    assert source.read_bytes() == b"video fixture"


@pytest.mark.parametrize(
    "value", ["../external", "/etc/passwd", "a" * 31, str(uuid4())]
)
def test_asset_tool_cannot_resolve_arbitrary_host_paths(tmp_path, value):
    with pytest.raises(ConfigError, match="Invalid asset"):
        asset_path(profile(tmp_path), value)


def test_asset_symlink_cannot_escape_owned_root(tmp_path):
    config = profile(tmp_path)
    (config.root / "assets").mkdir(parents=True)
    source = tmp_path / "private.txt"
    source.write_text("secret")
    (config.root / "assets" / ("a" * 32)).symlink_to(source)
    with pytest.raises(ConfigError):
        asset_path(config, "a" * 32)


def test_untimed_text_does_not_gain_visual_alignment(tmp_path):
    path = tmp_path / "transcript.txt"
    path.write_text("生抽两勺。")
    rows = partition_transcript(parse_transcript(path, ".txt"), 90, 30)
    assert len(rows) == 1
    assert rows[0]["start"] is None and rows[0]["end"] is None
    assert rows[0]["timing_precision"] == "unknown"


def test_subtitle_intervals_and_overlap_are_preserved(tmp_path):
    path = tmp_path / "transcript.srt"
    path.write_text("1\n00:00:29,000 --> 00:00:31,000\n生抽两勺。\n\n")
    rows = partition_transcript(parse_transcript(path, ".srt"), 40, 30)
    assert [row["transcript"] for row in rows] == ["生抽两勺。", "生抽两勺。"]
    path.write_text("1\n00:00:31,000 --> 00:00:29,000\ninvalid\n")
    with pytest.raises(ValueError, match="interval"):
        parse_transcript(path, ".srt")


def test_unknown_numbers_and_conflicts_are_never_promoted(tmp_path):
    raw = {
        "facts": [
            {
                "statement": "盐3克",
                "value": "3",
                "quote": "盐?克",
                "evidence_kind": "visual",
                "uncertain": False,
            },
            {
                "statement": "生抽2勺",
                "value": "2",
                "quote": "生抽2勺",
                "evidence_kind": "transcript",
                "uncertain": False,
                "conflict": True,
            },
            {
                "statement": "糖5克",
                "value": "5",
                "quote": "糖5克",
                "evidence_kind": "transcript",
                "uncertain": False,
            },
        ]
    }
    result = validate_extraction(raw, "生抽2勺", visual=True)
    assert len(result["facts"]) == 2
    assert result["facts"][0]["value"] is None and result["facts"][0]["uncertain"]
    assert "3" not in result["facts"][0]["statement"]
    assert result["facts"][1]["conflict"]
    assert len(validate_extraction(raw, "生抽2勺", visual=False)["facts"]) == 1


def test_dtype_and_checkpoint_changes_separate_video_vector_spaces(tmp_path):
    config = profile(tmp_path)
    assert config.space_id != replace(config, dtype="float16").space_id
    assert config.space_id != replace(config, checkpoint_sha256="different").space_id


def test_fusion_uses_ranks_not_incompatible_similarity_scales():
    a = {"document_id": "a", "section": "0", "semantic_score": 0.8}
    b = {"document_id": "b", "section": "0", "semantic_score": 0.1}
    c = {"document_id": "c", "section": "0", "semantic_score": 1000}
    rows = reciprocal_rank_fusion([[a, b], [b, c]], 3)
    assert [r["document_id"] for r in rows] == ["b", "a", "c"]
    assert rows[0]["semantic_score"] == 0.1


def test_extended_tools_default_to_unified_library_and_allow_explicit_scope(
    tmp_path,
):
    import asyncio

    config = default_config(AppPaths.discover())
    server = create_video_mcp(config, profile(tmp_path))
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == {
        "rag_search",
        "rag_status",
        "document_read",
        "video_import",
        "ingestion_status",
        "evidence_get",
        "favorites_import",
        "favorites_status",
    }
    definitions = {tool.name: tool for tool in tools}
    assert definitions["rag_search"].annotations.readOnlyHint
    assert definitions["favorites_status"].annotations.readOnlyHint
    assert not definitions["favorites_import"].annotations.readOnlyHint
    assert not definitions["video_import"].annotations.readOnlyHint
    for name in (
        "rag_search",
        "video_import",
        "favorites_import",
        "favorites_status",
        "evidence_get",
    ):
        schema = definitions[name].inputSchema
        assert "knowledge_base" not in schema.get("required", [])
        assert schema["properties"]["knowledge_base"]["default"] == "all"


def test_numeric_review_preserves_previous_facts_and_both_conflict_sources():
    original = validate_extraction(
        {
            "facts": [
                {
                    "statement": "生抽2勺",
                    "entity": "生抽",
                    "attribute": "amount",
                    "unit": "勺",
                    "value": "2",
                    "quote": "生抽2勺",
                    "evidence_kind": "transcript",
                },
                {
                    "statement": "生抽?勺",
                    "entity": "生抽",
                    "attribute": "amount",
                    "unit": "勺",
                    "quote": "生抽?勺",
                    "evidence_kind": "visual",
                    "uncertain": True,
                },
            ]
        },
        "生抽2勺",
        visual=True,
    )
    reviewed = validate_extraction(
        {
            "facts": [
                {
                    "statement": "生抽3勺",
                    "entity": "生抽",
                    "attribute": "amount",
                    "unit": "勺",
                    "value": "3",
                    "quote": "生抽3勺",
                    "evidence_kind": "visual",
                },
                {
                    "statement": "盐5克",
                    "entity": "盐",
                    "attribute": "amount",
                    "unit": "克",
                    "value": "5",
                    "quote": "盐5克",
                    "evidence_kind": "visual",
                },
            ]
        },
        "生抽2勺",
        visual=True,
    )
    result = merge_numeric_review(original, reviewed)
    assert len(result["facts"]) == 3
    assert result["facts"][0]["conflict"] and result["facts"][2]["conflict"]
    assert result["facts"][1]["uncertain"]
    assert result["facts"][2]["review"] and result["facts"][2]["frame_index"] == 4
    assert all(f["entity"] != "盐" for f in result["facts"])


def test_cleanup_integrity_failure_never_deletes_source(tmp_path, monkeypatch):
    import json

    config = profile(tmp_path)
    source = tmp_path / "external.mp4"
    source.write_bytes(b"original media")
    asset = stage_asset(source, config)
    job_id = str(uuid4())
    derived = config.root / "derived" / job_id
    derived.mkdir(parents=True)
    evidence = derived / "0-0.jpg"
    evidence.write_bytes(b"corrupt")
    (derived / "record.json").write_text(
        json.dumps(
            {
                "id": job_id,
                "segments": [
                    {
                        "evidence": [
                            {
                                "relative_path": str(evidence.relative_to(config.root)),
                                "sha256": "expected",
                            }
                        ]
                    }
                ],
            }
        )
    )
    with pytest.raises(RuntimeError, match="Evidence verification"):
        cleanup(
            None, config, {"id": job_id, "payload": {"asset_id": asset["asset_id"]}}
        )
    assert asset_path(config, asset["asset_id"])[0].is_file()
    assert source.read_bytes() == b"original media"


def test_cleanup_can_resume_after_source_removed_before_final_commit(
    tmp_path, monkeypatch
):
    import json
    from contextlib import contextmanager

    from rag_favorite import video_worker

    config = profile(tmp_path)
    source = tmp_path / "external.mp4"
    source.write_bytes(b"original media")
    asset = stage_asset(source, config)
    job_id = str(uuid4())
    derived = config.root / "derived" / job_id
    derived.mkdir(parents=True)
    (derived / "record.json").write_text(
        json.dumps({"id": job_id, "segments": [{"evidence": []}]})
    )
    job = {"id": job_id, "payload": {"asset_id": asset["asset_id"]}}

    class Connection:
        def execute(self, sql, params):
            self.count = "count(*)" in sql
            return self

        def fetchone(self):
            return (1,) if self.count else None

    @contextmanager
    def database(_):
        yield Connection()

    monkeypatch.setattr(video_worker, "connect_database", database)

    def interrupted(*_):
        raise RuntimeError("interrupted before final commit")

    monkeypatch.setattr(video_worker, "finish_cleanup", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        cleanup(None, config, job)
    assert not (config.root / "assets" / asset["asset_id"]).exists()
    completed = []
    monkeypatch.setattr(
        video_worker, "finish_cleanup", lambda *_: completed.append(True)
    )
    cleanup(None, config, job)
    assert completed == [True] and source.is_file()


def test_backfill_inventory_preserves_manual_notes_and_marks_missing_media(tmp_path):
    from rag_favorite.video_backfill import inventory

    root = tmp_path / "legacy"
    root.mkdir()
    manual = root / "recipe.md"
    manual.write_text("人工正文\n旧摘要\n[视频](missing.mp4)\n")
    before = manual.read_bytes()
    (root / "available.mp4").write_bytes(b"fixture")
    (root / "available.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n菜谱\n")
    (root / "old.txt").write_text("旧转录，媒体已删除")
    result = inventory(root)
    assert manual.read_bytes() == before
    assert result["notes"][0]["state"] == "source_missing_text_only"
    assert result["videos"][0]["transcript"] == "available.srt"
    assert result["orphan_transcripts"][0]["state"] == "source_missing_text_only"


def test_visual_budget_stops_before_extra_request(tmp_path, monkeypatch):
    from rag_favorite.video_provider import CCRProvider, ProviderUnavailable

    provider = CCRProvider(profile(tmp_path))
    provider.start_visual_budget()
    provider.visual_images = 99
    with pytest.raises(ProviderUnavailable, match="REQUEST_BUDGET"):
        provider.check_budget(2)
    provider.visual_started -= 36_000
    provider.check_budget(1)


def test_explicit_time_budget_remains_an_opt_in(tmp_path):
    from rag_favorite.video_provider import CCRProvider, ProviderUnavailable

    provider = CCRProvider(replace(profile(tmp_path), visual_budget_seconds=600))
    provider.start_visual_budget()
    provider.visual_started -= 601
    with pytest.raises(ProviderUnavailable, match="TIME_BUDGET"):
        provider.check_budget()


@pytest.mark.parametrize("budget", [0, 600])
def test_failed_visual_stream_is_accounted_with_unlimited_or_explicit_budget(
    tmp_path, monkeypatch, budget
):
    import io
    import json

    from rag_favorite import video_provider

    config = replace(profile(tmp_path), visual_budget_seconds=budget)
    monkeypatch.setattr(type(config), "secret", lambda *_: "test-token")
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text(json.dumps({"images": 1, "seconds": 535}) + "\n")
    image = tmp_path / "image.jpg"
    image.write_bytes(b"test")
    timeouts = []

    def response(request, timeout):
        timeouts.append(timeout)
        return io.BytesIO(b'data: {"type":"response.incomplete"}\n')

    monkeypatch.setattr(video_provider.urllib.request, "urlopen", response)
    provider = video_provider.CCRProvider(config, ledger)
    provider.start_visual_budget()
    with pytest.raises(video_provider.ProviderUnavailable) as error:
        provider.json("extract", "input", [image])
    assert error.value.code == "CCR_RESPONSE_INCOMPLETE"
    if budget:
        assert 1 <= timeouts[0] <= 65
    else:
        assert timeouts == [600]
    loaded = video_provider.CCRProvider(config, ledger)
    assert loaded.visual_images == 2 and loaded.visual_requests == 2
    assert loaded.calls[-1]["completed"] is False


def test_unlimited_visual_job_can_resume_after_previous_time_budget_exhausted(
    tmp_path, monkeypatch
):
    import io
    import json

    from rag_favorite import video_provider

    config = profile(tmp_path)
    monkeypatch.setattr(type(config), "secret", lambda *_: "test-token")
    ledger = tmp_path / "usage.jsonl"
    previous = {"images": 4, "seconds": 3600, "completed": True}
    ledger.write_text(json.dumps(previous) + "\n")
    image = tmp_path / "image.jpg"
    image.write_bytes(b"image fixture")
    timeouts = []
    payload = {"caption": "supported screen", "facts": [], "edges": []}
    events = [
        {"type": "response.output_text.delta", "delta": json.dumps(payload)},
        {"type": "response.completed", "response": {"usage": {}}},
    ]

    def response(request, timeout):
        timeouts.append(timeout)
        return io.BytesIO(
            "".join("data: " + json.dumps(e) + "\n" for e in events).encode()
        )

    monkeypatch.setattr(video_provider.urllib.request, "urlopen", response)
    provider = video_provider.CCRProvider(config, ledger)
    provider.start_visual_budget()
    provider.visual_started -= 3600
    assert provider.json("extract", "input", [image]) == payload
    assert timeouts == [600]
    saved = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert saved[0] == previous and len(saved) == 2 and saved[1]["completed"]
    loaded = video_provider.CCRProvider(config, ledger)
    assert loaded.visual_images == 5 and loaded.previous_visual_seconds >= 3600


@pytest.mark.parametrize("seconds", [None, 0, 600, -1, 1, 3601])
def test_video_config_accepts_unlimited_default_and_validates_explicit_limits(
    tmp_path, seconds
):
    from rag_favorite.video_config import load_video_config

    path = tmp_path / "video.toml"
    path.write_text("" if seconds is None else f"visual_budget_seconds = {seconds}\n")
    if seconds in {-1, 1, 3601}:
        with pytest.raises(ConfigError):
            load_video_config(path)
    else:
        config = load_video_config(path)
        assert config.visual_budget_seconds == (seconds or 0)
        assert config.space_id == replace(config, visual_budget_seconds=600).space_id


def test_final_gold_rejects_duplicate_content_as_ten_videos(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from rag_favorite import video_evaluation

    ids = [str(uuid4()) + ":0" for _ in range(10)]
    questions = [
        {
            "query": f"问题 {n}",
            "expected_answer": "人工核对的标准答案",
            "collection": "cooking",
            "expected_segment_ids": [ids[n % 10]],
            "human_verified": True,
        }
        for n in range(60)
    ]

    class Connection:
        def execute(self, *_):
            return self

        def fetchall(self):
            return [
                (s, s.split(":")[0], "cooking", {"cooking": True}, "same-content")
                for s in ids
            ]

    @contextmanager
    def database(_):
        yield Connection()

    monkeypatch.setattr(video_evaluation, "connect_database", database)
    with pytest.raises(ConfigError, match="different sources"):
        video_evaluation.evaluate(
            None, profile(tmp_path), {"questions": questions}, final=True
        )


def test_chunked_asr_offsets_words_and_reuses_completed_chunks(tmp_path, monkeypatch):
    from rag_favorite import video_worker

    model = tmp_path / "model"
    model.mkdir()
    (model / "model.bin").write_bytes(b"pinned fixture model")
    config = replace(profile(tmp_path), asr_model=model)
    work = config.root / "work"
    work.mkdir(parents=True)
    calls = []
    progress = []

    def encoder(config, operation, payload):
        if operation == "probe":
            return {"duration": 601}
        calls.append(payload["path"])
        return {
            "segments": [
                {
                    "start": 0.2,
                    "end": 0.8,
                    "text": "字幕",
                    "words": [{"start": 0.3, "end": 0.5, "text": "字幕"}],
                }
            ]
        }

    monkeypatch.setattr(video_worker, "local_encoder", encoder)
    monkeypatch.setattr(
        video_worker,
        "ffmpeg",
        lambda config, args: __import__("pathlib").Path(args[-1]).write_bytes(b"audio"),
    )
    first = video_worker.transcribe_chunks(
        config,
        tmp_path / "source.mp4",
        work,
        "source-sha",
        progress=lambda n, total: progress.append((n, total)),
    )
    second = video_worker.transcribe_chunks(
        config, tmp_path / "source.mp4", work, "source-sha"
    )
    assert len(calls) == 3 and first == second
    assert progress == [(1, 3), (2, 3), (3, 3)]
    assert first["segments"][2]["words"][0]["start"] == 600.3
    assert not list(work.glob("audio-*.wav"))


@pytest.mark.parametrize(
    "url",
    [
        "http://xhslink.cn/o/example",
        "https://127.0.0.1/o/example",
        "https://xhslink.cn.evil.test/o/example",
        "https://user:secret@xhslink.cn/o/example",
        "https://www.xiaohongshu.com/user/profile/123",
        "https://xhslink.cn:1234/o/example",
    ],
)
def test_source_url_cannot_read_private_or_unrelated_destinations(url):
    from rag_favorite.video_sources import validate_source_url

    with pytest.raises(ConfigError):
        validate_source_url(url)


def test_quality_evaluation_rejects_unreviewed_model_labels():
    from rag_favorite.video_evaluation import validate_gold

    question = {
        "query": "生抽多少",
        "collection": "cooking",
        "expected_segment_ids": [str(uuid4()) + ":0"],
        "human_verified": False,
    }
    with pytest.raises(ConfigError, match="human"):
        validate_gold({"questions": [question]})
    question["human_verified"] = True
    assert validate_gold({"questions": [question]}) == [question]
    with pytest.raises(ConfigError, match="60"):
        validate_gold({"questions": [question]}, final=True)


def test_source_prefers_muxed_audio_over_a_larger_silent_dash_stream():
    from rag_favorite.xhs_note import preferred_muxed_stream

    silent = {
        "masterUrl": "https://example.xhscdn.com/silent",
        "audioChannels": 0,
        "height": 2160,
        "avgBitrate": 5000000,
    }
    muxed = {
        "masterUrl": "https://example.xhscdn.com/muxed",
        "audioChannels": 2,
        "height": 1080,
        "avgBitrate": 2900000,
    }
    small = {
        "masterUrl": "https://example.xhscdn.com/small",
        "audioChannels": 2,
        "height": 720,
        "avgBitrate": 1300000,
    }
    assert (
        preferred_muxed_stream({"video": {"streams": [silent, muxed, small]}}) is muxed
    )


def test_joint_text_ranking_does_not_give_a_weak_document_an_equal_first_rank():
    from rag_favorite.video_retrieval import common_text_ranking

    document = {"document_id": "doc", "section": "0", "semantic_score": 0.1}
    video = {"document_id": "video", "section": "0", "semantic_score": 0.8}
    assert common_text_ranking([document], [video]) == [video, document]


def test_asr_quantity_without_visual_support_remains_unknown():
    from rag_favorite.video_worker import crosscheck_asr_quantities

    facts = validate_extraction(
        {
            "facts": [
                {
                    "statement": "蛋黄两克",
                    "entity": "蛋黄",
                    "attribute": "amount",
                    "value": "两",
                    "unit": "克",
                    "quote": "两克的蛋黄",
                    "evidence_kind": "transcript",
                }
            ]
        },
        "两克的蛋黄",
        visual=True,
    )
    checked = crosscheck_asr_quantities(facts)
    assert checked["facts"][0]["uncertain"] and checked["facts"][0]["value"] is None
    assert checked["facts"][0]["uncertainty_reason"] == "unverified_asr_quantity"


def test_different_units_preserve_both_conflicting_sources():
    raw = {
        "facts": [
            {
                "statement": "蛋黄两克",
                "entity": "蛋黄",
                "attribute": "amount",
                "value": "两",
                "unit": "克",
                "quote": "两克的蛋黄",
                "evidence_kind": "transcript",
            },
            {
                "statement": "两个蛋黄",
                "entity": "蛋黄",
                "attribute": "amount",
                "value": "两",
                "unit": "个",
                "quote": "两个蛋黄",
                "evidence_kind": "visual",
                "frame_index": 4,
            },
        ]
    }
    result = validate_extraction(raw, "两克的蛋黄", visual=True)
    assert all(f["conflict"] for f in result["facts"])


def test_numeric_frame_sampling_covers_spoken_quantity_time():
    from rag_favorite.video_worker import numeric_frame_times

    transcript = [
        {"start": 13.88, "end": 14.88, "text": "两克的蛋黄"},
        {"start": 32, "end": 33, "text": "生抽2勺"},
    ]
    assert numeric_frame_times(transcript, {"start": 0, "end": 30}) == [14.38]


def test_favorites_mcp_background_passes_both_profiles(tmp_path, monkeypatch):
    import asyncio
    import subprocess
    from dataclasses import replace

    config = default_config(AppPaths.discover())
    video = replace(profile(tmp_path), source=tmp_path / "video.toml")
    calls = []
    monkeypatch.setattr(
        subprocess, "Popen", lambda command, **kwargs: calls.append((command, kwargs))
    )
    server = create_video_mcp(config, video)
    asyncio.run(server.call_tool("favorites_import", {"knowledge_base": "cooking"}))
    assert len(calls) == 1
    command, arguments = calls[0]
    assert command[-2:] == ["--collection", "cooking"]
    assert arguments["env"]["RAG_FAVORITE_CONFIG"] == str(config.source)
    assert arguments["env"]["RAG_VIDEO_CONFIG"] == str(video.source)
    assert arguments["start_new_session"]
    assert (video.root / "favorites").stat().st_mode & 0o077 == 0
