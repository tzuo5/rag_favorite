from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from backend.ingestion.openrouter_asr import decode_transcription
from backend.ingestion.temporal_models import (
    TranscriptResult,
    TranscriptSegment,
    TranscriptWord,
    format_time,
    from_markdown,
    parse_subtitles,
    parse_time,
)
from backend.transcriber import Transcriber
from backend.video_processor import VideoProcessor
from pydantic import ValidationError


def test_subtitles_keep_hours_milliseconds_and_distant_repeats(tmp_path):
    text = """WEBVTT

00:00:01.123 --> 00:00:03.456
加入

00:00:02.000 --> 00:00:04.789
加入酱汁

00:05:02.000 --> 00:05:04.789
加入酱汁

01:02:03.456 --> 01:02:04.789
生抽 2 勺
"""
    entries = parse_subtitles(text)
    assert [e["text"] for e in entries] == ["加入酱汁", "加入酱汁", "生抽 2 勺"]
    assert entries[0]["start"] == 1.123
    assert entries[-1]["start"] == 3723.456
    path = tmp_path / "subtitle.vtt"
    path.write_text(text)
    processor = VideoProcessor()
    wrapper = processor._format_subtitle_entries(processor._parse_vtt(str(path)), "zh")
    result = from_markdown(wrapper)
    assert result.segments[-1].start == 3723.456
    assert result.language == "zh"
    assert result.checksum == from_markdown(result.to_markdown()).checksum


@pytest.mark.parametrize(
    "start,end", [(float("nan"), 1), (0, float("inf")), (-1, 0), (2, 1), (None, 1)]
)
def test_invalid_transcript_times_are_rejected(start, end):
    with pytest.raises(ValidationError):
        TranscriptWord(text="步骤", start=start, end=end)


def test_untimed_transcript_has_no_invented_zero_timestamp():
    result = from_markdown("没有时间的旧转录。")
    assert result.segments[0].start is None
    assert result.timing_precision == "unknown"
    assert "[00:00" not in result.to_markdown()
    assert parse_time("75:30") == 4530
    assert format_time(parse_time("01:02:03,456")) == "62:03.456"


def test_segment_words_must_be_inside_the_segment():
    with pytest.raises(ValidationError):
        TranscriptSegment(
            id="s",
            text="生抽",
            start=1,
            end=2,
            words=[TranscriptWord(text="生抽", start=0.5, end=1.5)],
        )


def test_asr_actual_word_timestamps_and_slice_offsets():
    result = decode_transcription(
        {
            "text": "加盐",
            "language": "zh",
            "segments": [
                {
                    "start": 1.123,
                    "end": 2.456,
                    "text": "加盐",
                    "words": [{"start": 1.123, "end": 2.456, "word": "加盐"}],
                }
            ],
        },
        offset=30,
        duration=10,
    )
    assert result.segments[0].start == 31.123
    assert result.segments[0].words[0].end == 32.456
    assert result.timing_precision == "word"


@pytest.mark.parametrize(
    "reliable,precision", [(True, "coarse"), (False, "approximate")]
)
def test_text_only_asr_uses_real_slice_bounds(reliable, precision):
    result = decode_transcription(
        {"text": "加盐和醋"}, offset=30, duration=25, reliable_offset=reliable
    )
    assert result.segments[0].start == 30
    assert result.segments[0].end == 55
    assert result.segments[0].words == []
    assert result.timing_precision == precision
    assert from_markdown(result.to_markdown()).timing_precision == precision


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "x", "segments": ["invalid"]},
        {"text": "x", "segments": [{"start": -1, "end": 2, "text": "x"}]},
        {"text": "x", "segments": [{"start": 0, "end": 40, "text": "x"}]},
        {"error": "bad"},
    ],
)
def test_bad_asr_does_not_invent_timing(payload):
    with pytest.raises(ValueError):
        decode_transcription(payload, offset=0, duration=30)


def test_whisper_wrapper_preserves_float_timestamps_without_loading_models(tmp_path):
    path = tmp_path / "audio.wav"
    path.write_bytes(b"fake fixture")
    transcriber = Transcriber()
    transcriber.model = SimpleNamespace(
        transcribe=lambda *a, **k: (
            iter([SimpleNamespace(start=1.123, end=2.456, text="加入酱汁")]),
            SimpleNamespace(language="zh", language_probability=1),
        )
    )
    result = asyncio.run(transcriber.transcribe_result(str(path)))
    assert isinstance(result, TranscriptResult)
    assert result.segments[0].start == 1.123
    assert "01.123" in asyncio.run(transcriber.transcribe(str(path)))
