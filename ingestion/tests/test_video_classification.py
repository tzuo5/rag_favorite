from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import yaml
from backend.ingestion.config import Settings
from backend.ingestion.content_classifier import (
    BatchAnalysis,
    ContentClassification,
    TextAnalysisClient,
    TranscriptAnalyzer,
    dispatch_visual,
    visual_gate,
)
from backend.ingestion.markdown import KnowledgeFileBuilder, fallback_enrichment
from backend.ingestion.media import CleanupManager, TranscriptService
from backend.ingestion.models import Platform, SourceMetadata
from backend.ingestion.repository import JobControlRequested
from backend.ingestion.service import VideoIngestionService
from backend.ingestion.temporal_models import from_markdown
from openai import BadRequestError
from pydantic import ValidationError


class FakeClient:
    model = "Codex API/gpt-6-luna"
    fingerprint = "fake-v1"

    def __init__(self, category="cooking", *, fail=False, bad_ids=False):
        self.category, self.fail, self.bad_ids = category, fail, bad_ids
        self.calls = []

    async def analyze(self, segments, metadata):
        self.calls.append(segments)
        if self.fail:
            raise TimeoutError("text provider unavailable")
        return BatchAnalysis(
            classification=ContentClassification(
                category=self.category,
                decision="eligible" if self.category == "cooking" else "not_eligible",
                evidence_segment_ids=[
                    "invented" if self.bad_ids else segments[0]["id"]
                ],
                reason="实际做菜步骤",
            ),
            enrichment=fallback_enrichment(
                "fixture", "切菜。将鸡肉煎熟。"
            ).model_dump(),
        )


def analyze(
    client,
    *,
    text="**[00:01.123 - 00:04.456]**\n先切鸡肉，然后加入生抽两勺。",
    cache_root=None,
    **kwargs,
):
    transcript = from_markdown(text)
    analyzer = TranscriptAnalyzer(client=client, cache_root=cache_root, **kwargs)
    return asyncio.run(
        analyzer.analyze(transcript, SourceMetadata(platform=Platform.TELEGRAM))
    ), transcript


def test_cooking_requires_valid_transcript_evidence():
    result, transcript = analyze(FakeClient())
    assert result.classification.decision == "eligible"
    assert result.classification.evidence_segment_ids == [transcript.segments[0].id]
    assert result.coverage_complete
    assert visual_gate(result, enabled=True, has_video=True)["eligible"]


@pytest.mark.parametrize(
    "category,failed,enabled,has_video",
    [
        ("other", False, True, True),
        ("cooking", True, True, True),
        ("cooking", False, False, True),
        ("cooking", False, True, False),
    ],
)
def test_all_five_visual_operations_remain_zero_for_rejected_inputs(
    category, failed, enabled, has_video
):
    result, _ = analyze(FakeClient(category, fail=failed))
    calls = {
        role: 0
        for role in ("visual_download", "scene", "frames", "vlm", "visual_embedding")
    }

    async def processor(_):
        for role in calls:
            calls[role] += 1
        return {"status": "success"}

    asyncio.run(
        dispatch_visual(
            result, enabled=enabled, has_video=has_video, processor=processor
        )
    )
    assert calls == dict.fromkeys(calls, 0)


def test_positive_visual_seam_runs_once_without_changing_collection():
    result, _ = analyze(FakeClient())
    calls = []

    async def processor(analysis):
        calls.append(analysis)
        return {"status": "success"}

    assert (
        asyncio.run(
            dispatch_visual(result, enabled=True, has_video=True, processor=processor)
        )["status"]
        == "success"
    )
    assert calls == [result]


def test_schema_rejection_retries_json_only_on_the_same_route(monkeypatch):
    monkeypatch.setenv("OPENAI_API_STYLE", "responses")
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            response = httpx.Response(
                400, request=httpx.Request("POST", "https://example.com/responses")
            )
            raise BadRequestError(
                "json_schema is unsupported", response=response, body=None
            )
        return SimpleNamespace(
            output_text=json.dumps(
                {
                    "classification": {
                        "category": "unknown",
                        "decision": "uncertain",
                        "reason": "unclear",
                    }
                }
            )
        )

    client = TextAnalysisClient(
        client=SimpleNamespace(responses=SimpleNamespace(create=create))
    )
    asyncio.run(client.analyze([{"id": "s1", "text": "内容"}], SourceMetadata()))
    assert len(calls) == 2
    assert calls[0]["model"] == calls[1]["model"]
    assert "text" in calls[0] and "text" not in calls[1]


@pytest.mark.parametrize(
    "category,enabled,video,expected",
    [
        ("other", True, True, "not_eligible_category"),
        ("cooking", False, True, "disabled"),
        ("cooking", True, False, "audio_only"),
        ("cooking", True, None, "video_track_unverified"),
    ],
)
def test_negative_visual_gates(category, enabled, video, expected):
    result, _ = analyze(FakeClient(category))
    gate = visual_gate(result, enabled=enabled, has_video=video)
    assert gate["eligible"] is False
    assert gate["reason"] == expected


def test_fabricated_segment_ids_never_enable_visuals():
    result, _ = analyze(FakeClient(bad_ids=True))
    assert result.classification.category == "unknown"
    assert not visual_gate(result, enabled=True, has_video=True)["eligible"]


@pytest.mark.parametrize(
    "category,decision",
    [("unknown", "eligible"), ("other", "eligible"), ("cooking", "eligible")],
)
def test_conflicting_or_unsupported_classifications_are_rejected(category, decision):
    with pytest.raises(ValidationError):
        ContentClassification(category=category, decision=decision)


def test_cache_reuses_exact_transcript_and_invalidates_model_or_text(tmp_path):
    client = FakeClient()
    first, transcript = analyze(client, cache_root=tmp_path)
    assert first.classification.category == "cooking"
    failed = FakeClient(fail=True)
    cached, _ = analyze(failed, cache_root=tmp_path)
    assert cached.cache_hit and failed.calls == []
    changed, _ = analyze(failed, text="修改过的转录。", cache_root=tmp_path)
    assert changed.classification.category == "unknown" and not changed.cache_hit
    failed.fingerprint = "fake-v2"
    changed_model, _ = analyze(failed, cache_root=tmp_path)
    assert changed_model.classification.category == "unknown"
    analyzer = TranscriptAnalyzer(
        client=client, cache_root=tmp_path, allowed_categories=()
    )
    assert analyzer.cache_key(transcript) not in {
        path.stem for path in tmp_path.iterdir()
    }


def test_long_transcript_checks_late_cooking_and_partial_coverage():
    class LateClient(FakeClient):
        async def analyze(self, segments, metadata):
            self.category = (
                "cooking" if any("切肉" in s["text"] for s in segments) else "other"
            )
            self.fail = any("故障" in s["text"] for s in segments)
            return await super().analyze(segments, metadata)

    result, _ = analyze(
        LateClient(), text="普通内容。" * 30 + "切肉煎熟。", batch_characters=20
    )
    assert result.classification.category == "cooking" and result.coverage_complete
    partial, _ = analyze(
        LateClient(), text="普通内容。" * 10 + "故障。", batch_characters=20
    )
    assert (
        partial.classification.category == "unknown" and not partial.coverage_complete
    )


def test_broken_summary_does_not_discard_valid_category():
    class BrokenSummary(FakeClient):
        async def analyze(self, segments, metadata):
            result = await super().analyze(segments, metadata)
            result.enrichment = {"summary": "broken"}
            return result

    result, _ = analyze(BrokenSummary())
    assert result.classification.category == "cooking"
    assert len(result.enrichment.tags) >= 8


def test_job_control_propagates_outside_api_fallback():
    analyzer = TranscriptAnalyzer(client=FakeClient(fail=True))

    def check():
        raise JobControlRequested("CANCELLED")

    with pytest.raises(JobControlRequested):
        asyncio.run(
            analyzer.analyze(
                from_markdown("切菜"), SourceMetadata(), control_check=check
            )
        )


@pytest.mark.parametrize("style", ["responses", "chat"])
def test_text_protocol_is_explicit_and_contains_no_images(monkeypatch, style):
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        content = json.dumps(
            {
                "classification": {
                    "category": "other",
                    "decision": "not_eligible",
                    "evidence_segment_ids": [],
                    "reason": "探店",
                },
                "enrichment": None,
            }
        )
        return SimpleNamespace(
            output_text=content,
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        )

    monkeypatch.setenv("OPENAI_API_STYLE", style)
    monkeypatch.setenv("OPENAI_MODEL", "Codex API/gpt-6-luna")
    client = TextAnalysisClient(
        client=SimpleNamespace(
            responses=SimpleNamespace(create=create),
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        )
    )
    asyncio.run(client.analyze([{"id": "s1", "text": "餐厅探店"}], SourceMetadata()))
    assert calls[0]["model"] == "Codex API/gpt-6-luna"
    assert "image_url" not in json.dumps(calls)
    assert ("input" in calls[0]) == (style == "responses")


@pytest.mark.parametrize("category,failed", [("other", False), ("cooking", True)])
def test_stage_archives_transcript_preserves_metadata_and_never_probes_negative_media(
    tmp_path, monkeypatch, category, failed
):
    service = object.__new__(VideoIngestionService)
    service.settings = Settings(
        staging_root=tmp_path / "staging",
        temp_root=tmp_path / "temp",
        video_visual_enabled=True,
        cloud_cache_root=tmp_path / "cache",
    )
    service.builder = KnowledgeFileBuilder()
    service.cleanup = CleanupManager()
    service.transcripts = TranscriptService(service.settings)
    client = FakeClient(category, fail=failed)
    service.analyzer = TranscriptAnalyzer(client=client)
    job = {
        "id": "00000000-0000-0000-0000-000000000001",
        "state": "BUILDING_MARKDOWN",
        "telegram_chat_id": "123",
        "telegram_message_id": "456",
        "metadata": {"existing_control": "keep"},
        "notification_mode": "INDIVIDUAL",
        "destination_locked": False,
    }
    transitions = []
    service.sql = SimpleNamespace(
        get_job=lambda _: job, transition=lambda *a, **kw: transitions.append((a, kw))
    )
    service._notify_job = lambda *a, **kw: None
    monkeypatch.setattr(
        "backend.ingestion.service.probe_video_track",
        lambda *a: pytest.fail("Negative gate probed visual media"),
    )
    job_dir = tmp_path / "temp" / "job"
    job_dir.mkdir(parents=True)
    (job_dir / "source.mp4").write_bytes(b"fake")
    # A cooking destination must not override other or unavailable classification.
    job["selected_destination"] = "cooking"
    transcript = "**[00:01.123 - 00:04.456]**\n屏幕上的酱汁是一家餐厅的广告。"
    asyncio.run(
        service._stage(
            job, job_dir, SourceMetadata(platform=Platform.TELEGRAM), transcript, None
        )
    )
    staged = next(service.settings.staging_root.glob("*.md"))
    frontmatter = yaml.safe_load(staged.read_text().split("---\n")[1])
    assert frontmatter["visual_status"]["eligible"] is False
    assert transcript in staged.read_text()
    metadata = next(kw["metadata"] for _, kw in transitions if "metadata" in kw)
    assert metadata["existing_control"] == "keep"
    assert metadata["structured_transcript"]["segments"][0]["start"] == 1.123
    assert len(client.calls) == 1  # Classification and base enrichment share one call.
