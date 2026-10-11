from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from backend.ingestion.config import Settings
from backend.ingestion.media import TranscriptService
from backend.ingestion.model_usage_ledger import ModelUsageLedger
from backend.ingestion.models import SourceMetadata
from backend.ingestion.openrouter_asr import OpenRouterASR


def test_durable_unknown_cost_blocks_replay_after_restart(tmp_path):
    path = tmp_path / "usage.sqlite"
    ledger = ModelUsageLedger(path, maximum_usd=0.03)
    first = ledger.reserve("asr", "qwen", 0.01)
    ledger.settle(first, None)
    restarted = ModelUsageLedger(path, maximum_usd=0.03)
    assert restarted.summary()["unknown_requests"] == 1
    assert restarted.summary()["estimated_usd"] == 0.01
    with pytest.raises(ValueError, match="Unresolved"):
        restarted.reserve("asr", "qwen", 0.01)


def test_actual_cost_reconciliation_and_budget(tmp_path):
    ledger = ModelUsageLedger(tmp_path / "usage.sqlite", maximum_usd=0.03)
    first = ledger.reserve("asr", "qwen", 0.01)
    ledger.settle(first, {"cost": 0.02, "duration": 30})
    assert ledger.summary()["actual_usd"] == 0.02
    with pytest.raises(ValueError, match="exhausted"):
        ledger.reserve("asr", "qwen", 0.02)
    second = ledger.reserve("text_embedding", "pplx", 0.001)
    ledger.settle(second, {"cost": 0.02})
    assert ledger.summary()["overrun"]


def test_asr_protocol_caching_and_charge_accounting(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-secret")
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert str(request.url).endswith("/audio/transcriptions")
        assert body["model"] == "qwen/qwen3-asr-0.6b"
        assert body["input_audio"]["format"] == "wav"
        assert "provider" not in body
        return httpx.Response(200, json={"text": "加生抽", "usage": {"cost": 0.0001}})

    settings = Settings(
        cloud_cache_root=tmp_path / "cache", video_asr_backend="openrouter"
    )
    asr = OpenRouterASR(settings, transport=httpx.MockTransport(respond))
    path = tmp_path / "slice.wav"
    path.write_bytes(b"fixture audio")
    ledger = ModelUsageLedger(tmp_path / "usage.sqlite")
    first = asyncio.run(
        asr.transcribe_slice(path, offset=30, duration=30, ledger=ledger)
    )
    second = asyncio.run(
        asr.transcribe_slice(path, offset=30, duration=30, ledger=ledger)
    )
    assert first == second and first.timing_precision == "coarse"
    assert len(requests) == ledger.summary()["requests"] == 1
    assert ledger.summary()["actual_usd"] == 0.0001
    assert "fixture-secret" not in json.dumps(ledger.summary())


@pytest.mark.parametrize("status", [400, 429, 500])
def test_failed_asr_requests_do_not_automatically_repeat(tmp_path, monkeypatch, status):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"error": "failed"})

    settings = Settings(
        cloud_cache_root=tmp_path / "cache", video_asr_backend="openrouter"
    )
    asr = OpenRouterASR(settings, transport=httpx.MockTransport(respond))
    path = tmp_path / "slice.wav"
    path.write_bytes(b"audio")
    ledger = ModelUsageLedger(tmp_path / "usage.sqlite")
    with pytest.raises(ValueError, match=f"HTTP {status}"):
        asyncio.run(asr.transcribe_slice(path, offset=0, duration=30, ledger=ledger))
    with pytest.raises(ValueError, match="Unresolved"):
        asyncio.run(asr.transcribe_slice(path, offset=0, duration=30, ledger=ledger))
    assert len(requests) == 1


def test_missing_cloud_key_does_not_import_or_load_whisper(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    settings = Settings(cloud_cache_root=tmp_path, video_asr_backend="openrouter")
    service = TranscriptService(settings)
    with pytest.raises(ValueError, match="credential"):
        asyncio.run(service.local_transcript(tmp_path / "input.mp4"))


def test_subtitles_skip_asr_and_audio_download(tmp_path, monkeypatch):
    service = TranscriptService(Settings(video_asr_backend="openrouter"))

    async def subtitles(*args):
        return "字幕全文", "title", "zh"

    async def forbidden(*args):
        pytest.fail("Subtitle hit started audio/ASR work")

    monkeypatch.setattr(service.video_processor, "fetch_subtitles", subtitles)
    monkeypatch.setattr(service.video_processor, "download_and_convert", forbidden)
    monkeypatch.setattr(service, "local_transcript", forbidden)
    result = asyncio.run(
        service.url_transcript("https://example.com/video", tmp_path, SourceMetadata())
    )
    assert result == ("字幕全文", "zh", True)


def test_invalid_cloud_endpoint_and_slice_size_fail_before_network(tmp_path):
    settings = Settings(cloud_cache_root=tmp_path)
    with pytest.raises(ValueError, match="HTTPS"):
        OpenRouterASR(replace(settings, openrouter_base_url="http://example.com"))
    with pytest.raises(ValueError, match="30"):
        OpenRouterASR(replace(settings, video_asr_chunk_seconds=31))
