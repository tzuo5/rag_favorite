"""Unified-library regressions: speech, topic scope and retained evidence boundaries."""

import asyncio
import json
import signal
import sys
from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

import pytest

from rag_favorite import (
    video_evaluation,
    video_mcp,
    video_retrieval,
    video_worker,
    xhs_favorites,
)
from rag_favorite.config import ConfigError, default_config
from rag_favorite.paths import AppPaths
from rag_favorite.video_config import VideoConfig
from rag_favorite.video_provider import ProviderUnavailable
from rag_favorite.video_store import (
    digest_file,
    ingestion_collection,
    library_id,
    stage_asset,
)
from rag_favorite.video_transcript import partition_transcript


@pytest.fixture
def profiles(tmp_path):
    return default_config(AppPaths.discover()), VideoConfig(
        root=tmp_path / "owned",
        credentials_file=tmp_path / "secret.env",
        asr_model=tmp_path / "model",
        visual_enabled=True,
    )


def test_default_import_has_one_compatible_storage_target(profiles):
    config, video = profiles
    assert ingestion_collection(config, video) == "cooking"
    assert ingestion_collection(config, video, "all") == "cooking"
    assert ingestion_collection(config, video, "tech") == "tech"
    with pytest.raises(ConfigError):
        ingestion_collection(config, video, "nonexistent")


def test_final_evaluation_accepts_mixed_topics_in_unified_library(
    profiles, monkeypatch
):
    config, video = profiles
    ids = [str(uuid4()) + ":0" for _ in range(10)]
    questions = [
        {
            "query": f"问题 {n}",
            "collection": "all",
            "expected_segment_ids": [ids[n % 10]],
            "expected_answer": "人工答案",
            "human_verified": True,
        }
        for n in range(60)
    ]

    class Connection:
        def execute(self, *args):
            return self

        def fetchall(self):
            return [
                (s, s.split(":")[0], "tech", {"mode": "unified"}, f"source-{n}")
                for n, s in enumerate(ids)
            ]

    @contextmanager
    def database(_):
        yield Connection()

    def search(query, collections, *args, **kwargs):
        assert collections == list(config.collections)
        assert args[0] == 5
        return [{"document_id": "video:" + ids[int(query.split()[1]) % 10].split(":")[0], "section": "summary"}]

    monkeypatch.setattr(video_evaluation, "connect_database", database)
    monkeypatch.setattr(video_evaluation, "search_all", search)
    result = video_evaluation.evaluate(
        config, video, {"questions": questions}, final=True
    )
    assert result["distinct_sources"] == 10 and result["top5_document_recall"] == 1


def test_no_speech_uses_visual_timeline_without_invented_transcript(
    profiles, monkeypatch
):
    _, video = profiles
    calls = []

    def no_speech(*args, **kwargs):
        calls.append(True)
        raise ProviderUnavailable("No speech", code="TRANSCRIPT_REQUIRED_NO_SPEECH")

    monkeypatch.setattr(video_worker, "transcribe_chunks", no_speech)
    work = video.root / "work"
    transcript, origin = video_worker.prepare_transcript(
        video,
        {"sha256": "source"},
        video.root / "video",
        work,
    )
    assert calls == [True] and transcript == [] and origin == "no_speech_visual_only"
    assert json.loads((work / "transcript.json").read_text())["no_speech"]
    segments = partition_transcript(transcript, 61, 30)
    assert [(s["start"], s["end"]) for s in segments] == [(0, 30), (30, 60), (60, 61)]
    raw = {
        "facts": [
            {
                "statement": "虚构语音",
                "quote": "虚构语音",
                "evidence_kind": "transcript",
            },
            {"statement": "画面字幕", "quote": "画面字幕", "evidence_kind": "visual"},
        ]
    }
    assert [
        f["statement"]
        for f in video_worker.validate_extraction(raw, "", visual=True)["facts"]
    ] == ["画面字幕"]


@pytest.mark.parametrize(
    "enabled,code",
    [
        (False, "TRANSCRIPT_REQUIRED_NO_SPEECH"),
        (True, "ENCODER_UNAVAILABLE"),
    ],
)
def test_asr_failures_are_not_silently_promoted_to_visual_success(
    profiles, monkeypatch, enabled, code
):
    _, video = profiles
    video = replace(video, visual_enabled=enabled)

    def failure(*args, **kwargs):
        raise ProviderUnavailable("failure", code=code)

    monkeypatch.setattr(video_worker, "transcribe_chunks", failure)
    with pytest.raises(ProviderUnavailable) as error:
        video_worker.prepare_transcript(
            video, {"sha256": "source"}, video.root / "video", video.root / "work"
        )
    assert error.value.code == code
    assert not (video.root / "work" / "transcript.json").exists()


def test_supplied_subtitles_still_take_precedence(profiles, monkeypatch, tmp_path):
    _, video = profiles
    path = tmp_path / "provided.srt"
    path.write_text("1\n00:00:00,000 --> 00:00:01,000\n技术教程字幕\n")
    manifest = stage_asset(path, video)
    monkeypatch.setattr(
        video_worker,
        "transcribe_chunks",
        lambda *a, **k: pytest.fail("ASR called despite subtitles"),
    )
    transcript, origin = video_worker.prepare_transcript(
        video,
        {"transcript_asset_id": manifest["asset_id"]},
        video.root / "video",
        video.root / "work",
    )
    assert transcript[0]["text"] == "技术教程字幕" and origin == "supplied_subtitle"


def test_old_text_only_checkpoint_cannot_skip_new_visual_processing():
    cached = {"cache_key": "old", "segment": {"video_embedding": None}}
    assert not video_worker.compatible_checkpoint(cached, "new", "old", visual=True)
    assert video_worker.compatible_checkpoint(cached, "new", "old", visual=False)
    cached["segment"]["video_embedding"] = [0.1]
    assert video_worker.compatible_checkpoint(cached, "new", "old", visual=True)
    assert not video_worker.compatible_checkpoint(cached, "new", "old", visual=False)
    cached["cache_key"] = "another-source"
    assert not video_worker.compatible_checkpoint(cached, "new", "old", visual=True)


def test_mcp_default_search_covers_every_configured_collection(profiles, monkeypatch):
    config, video = profiles
    selected = []

    def search(query, collections, *args, **kwargs):
        selected.append(collections)
        return []

    monkeypatch.setattr(video_mcp, "search_all", search)
    server = video_mcp.create_video_mcp(config, video)
    asyncio.run(server.call_tool("rag_search", {"query": "技术、社交和做菜资料"}))
    asyncio.run(
        server.call_tool("rag_search", {"query": "技术资料", "knowledge_base": "tech"})
    )
    assert selected == [list(config.collections), ["tech"]]


def test_mcp_default_favorites_status_resolves_existing_state(profiles, monkeypatch):
    config, video = profiles
    selected = []
    monkeypatch.setattr(
        xhs_favorites,
        "favorites_status",
        lambda v, key: selected.append(key) or {"state": "complete"},
    )
    asyncio.run(
        video_mcp.create_video_mcp(config, video).call_tool("favorites_status", {})
    )
    assert selected == [video.default_collection]


def test_unified_evidence_remains_owner_scoped_and_explicit_scope_is_respected(
    profiles, monkeypatch
):
    config, video = profiles
    job_id = str(uuid4())
    evidence_id = job_id + ":0:image:0"
    path = video.root / "derived" / job_id / "image.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"retained image")
    record = {
        "id": evidence_id,
        "relative_path": str(path.relative_to(video.root)),
        "sha256": digest_file(path),
    }

    class Connection:
        def execute(self, sql, params):
            assert (
                "library_id=%s" in sql
                and "j.state='complete'" in sql
                and "v.source_deleted" in sql
            )
            assert params[1] == library_id(video)
            self.visible = "tech" in params[2]
            return self

        def fetchone(self):
            return ([record],) if self.visible else None

    @contextmanager
    def database(_):
        yield Connection()

    monkeypatch.setattr(video_retrieval, "connect_database", database)
    assert video_retrieval.evidence_record(evidence_id, "all", config, video)[1] == path
    with pytest.raises(ConfigError, match="requested collection"):
        video_retrieval.evidence_record(evidence_id, "cooking", config, video)


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_worker_shutdown_unwinds_provider_finally(profiles, monkeypatch, signum):
    config, video = profiles
    finalized = []

    def worker(*args, **kwargs):
        try:
            signal.raise_signal(signum)
            pytest.fail("Shutdown signal ignored")
        finally:
            finalized.append(True)

    monkeypatch.setattr(sys, "argv", ["video-worker", "--once"])
    monkeypatch.setattr(video_worker, "load_config", lambda: config)
    monkeypatch.setattr(video_worker, "load_video_config", lambda: video)
    monkeypatch.setattr(video_worker, "run_worker", worker)
    handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        video_worker.main()
    finally:
        for s, handler in handlers.items():
            signal.signal(s, handler)
    assert finalized == [True]
