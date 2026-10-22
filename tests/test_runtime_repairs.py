"""Regressions for production console and terminal stream handling."""

import io
import json
import subprocess
from itertools import pairwise
from pathlib import Path
from typing import ClassVar

import pytest

from rag_favorite.video_config import VideoConfig
from rag_favorite.video_provider import CCRProvider
from rag_favorite.web_console import public_snapshot


def test_completed_response_does_not_wait_for_socket_close(tmp_path, monkeypatch):
    class TerminalStream(io.BytesIO):
        status = 200
        headers: ClassVar[dict] = {}

        def __iter__(self):
            yield b'data: {"type":"response.output_text.delta","delta":"{}"}\n'
            yield b'data: {"type":"response.completed","response":{"usage":{"total_tokens":9}}}\n'
            pytest.fail("client read again after terminal completion")

    monkeypatch.setenv("RAG_CCR_API_KEY", "test-secret")
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: TerminalStream())
    provider = CCRProvider(
        VideoConfig(root=tmp_path, credentials_file=tmp_path / "secret")
    )
    assert provider.json("instructions", "input") == {}
    assert provider.calls[-1]["usage"] == {"total_tokens": 9}


def test_inactive_worker_start_is_available():
    script = Path("src/rag_favorite/resources/web/console.js").read_text()
    script = script[: script.index('$("login-form").addEventListener')]
    harness = r"""
const vm = require("node:vm");
const elements = new Map();
const document = {getElementById(id) {if (!elements.has(id)) elements.set(id, {}); return elements.get(id);}, querySelectorAll() {return [];}};
const ctx = vm.createContext({document});
vm.runInContext(SCRIPT, ctx);
vm.runInContext('snapshot={control_supported:false, worker:{ActiveState:"inactive",MainPID:"0"},control:{paused:true}}; stale=false; controls();', ctx);
if (elements.get("start").disabled) throw new Error("Start disabled with inactive worker");
if (!elements.get("stop").disabled) throw new Error("Stop enabled without worker capability");
vm.runInContext('stale=true; controls();',ctx);
if (!elements.get("start").disabled) throw new Error("stale snapshot enabled Start");
for (const [job, expected] of [
  [{state:"running",pipeline_status:"waiting",paused:false},"queued"],
  [{state:"running",pipeline_status:"retry_wait",paused:true},"paused"],
  [{state:"running",pipeline_status:"running",paused:true},"running"],
  [{state:"queued",paused:true},"paused"],
  [{state:"blocked",pipeline_status:"blocked"},"blocked"]
]) {
  ctx.job = job;
  if (vm.runInContext("jobState(job)", ctx) !== expected) throw new Error("Incorrect stage filter");
}
""".replace("SCRIPT", json.dumps(script))
    result = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_pipeline_telemetry_is_public_but_private_values_are_removed():
    value = {
        "captured_at": "now",
        "counts": {},
        "total": 0,
        "paused_pending": 0,
        "control_supported": False,
        "truncated": False,
        "jobs": [],
        "models": [],
        "worker": {},
        "control": {},
        "logs": [],
        "pipeline": {
            "enabled": True,
            "stages": {"prepare": {"waiting": 2, "running": 1, "private": "secret"}},
            "private": "secret",
        },
        "call_metrics": {
            "http_success": 3,
            "searchable_completed": 8,
            "private": "secret",
        },
        "model_calls": [
            {"http_status": 200, "stream_complete": True, "private": "secret"}
        ],
    }
    result = public_snapshot(value)
    assert result["pipeline"]["stages"]["prepare"]["waiting"] == 2
    assert result["call_metrics"]["searchable_completed"] == 8
    assert result["model_calls"][0]["stream_complete"] is True
    assert "secret" not in json.dumps(result)


def test_short_download_cannot_be_published_as_asset(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from rag_favorite.video_provider import ProviderUnavailable
    from rag_favorite.video_sources import download_note_video

    class Response(io.BytesIO):
        url = "https://cdn.xhscdn.com/test.mp4"
        headers: ClassVar[dict] = {"Content-Length": "100"}

    monkeypatch.setattr(
        "urllib.request.build_opener",
        lambda *_a: SimpleNamespace(open=lambda *_a, **_k: Response(b"short")),
    )
    video = VideoConfig(root=tmp_path, credentials_file=tmp_path / "secret")
    with pytest.raises(ProviderUnavailable) as caught:
        download_note_video(
            video,
            {"media_url": Response.url, "note_id": "a" * 24},
            proxy="http://127.0.0.1:18998",
        )
    assert caught.value.code == "SOURCE_DOWNLOAD_INCOMPLETE"
    assert caught.value.retryable
    assert not list((tmp_path / "assets").iterdir())


def test_next_job_phrase_is_not_an_invented_dosage():
    from rag_favorite.knowledge_summaries import render_summary

    segment = {
        "ordinal": 0,
        "start": 0,
        "end": 10,
        "transcript": "这个职位是跳板",
        "facts": [],
    }
    raw = {
        "document_type": "职业建议",
        "sections": [
            {
                "heading": "建议",
                "paragraphs": [
                    {
                        "text": "把职位当作下一份工作的跳板。",
                        "citations": [{"segment": 0, "quote_id": "0:q0"}],
                    }
                ],
            }
        ],
    }
    assert "下一份工作" in render_summary(
        raw, title="职业", source="原文", segments=[segment]
    )
    raw["sections"][0]["paragraphs"][0]["text"] = "加一份盐。"
    with pytest.raises(ValueError, match="Unsupported summary number"):
        render_summary(raw, title="职业", source="原文", segments=[segment])


@pytest.mark.parametrize("duration,length", [(120.047166, 30), (240.042667, 60)])
def test_subsecond_tail_is_kept_in_last_useful_segment(duration, length):
    from rag_favorite.video_transcript import partition_transcript

    transcript = [{"start": duration - 0.03, "end": duration, "text": "尾段文字"}]
    segments = partition_transcript(transcript, duration, length)
    assert len(segments) == 4
    assert segments[-1]["end"] == duration
    assert "尾段文字" in segments[-1]["transcript"]
    assert all(s["end"] - s["start"] >= 1 for s in segments)
    assert segments[0]["start"] == 0
    assert all(a["end"] == b["start"] for a, b in pairwise(segments))


def test_merged_tail_does_not_reuse_a_shorter_segment_checkpoint():
    from rag_favorite.video_worker import compatible_checkpoint

    original = {"start": 90, "end": 120, "transcript": "正文"}
    saved = {"cache_key": "same", "segment": original}
    assert compatible_checkpoint(
        saved, "same", "same", visual=False, segment=dict(original)
    )
    assert not compatible_checkpoint(
        saved,
        "same",
        "same",
        visual=False,
        segment={"start": 90, "end": 120.047166, "transcript": "正文\n尾段文字"},
    )
