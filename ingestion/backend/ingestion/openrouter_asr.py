"""Cloud transcription of bounded local audio slices with honest timing precision."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .model_usage_ledger import ModelUsageLedger
from .temporal_models import (
    TranscriptResult,
    TranscriptSegment,
    TranscriptWord,
    segment_id,
)

MAX_AUDIO_BYTES = 25 * 1024 * 1024


def decode_transcription(
    payload: dict, *, offset: float, duration: float, reliable_offset: bool = True
) -> TranscriptResult:
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise ValueError("Invalid ASR response")
    if not all(math.isfinite(v) and v >= 0 for v in (offset, duration)):
        raise ValueError("Invalid audio slice interval")
    entries = payload.get("segments")
    segments = []
    if entries is not None and not isinstance(entries, list):
        raise ValueError("Invalid ASR segments")
    # Some providers expose top-level words without segments.
    if not entries and payload.get("words"):
        entries = [
            {
                "text": payload["text"],
                "start": 0,
                "end": duration,
                "words": payload["words"],
            }
        ]
    for index, raw in enumerate(entries or []):
        if not isinstance(raw, dict):
            raise ValueError("Invalid ASR segment")
        local = TranscriptWord.model_validate(
            {k: raw.get(k) for k in ("text", "start", "end")}
        )
        if local.start is None or local.end > duration + 0.001:
            raise ValueError("ASR timestamps lie outside the audio slice")
        raw_words = raw.get("words", [])
        if not raw_words and isinstance(payload.get("words"), list):
            raw_words = [
                w
                for w in payload["words"]
                if w.get("start", -1) >= local.start
                and w.get("end", duration + 1) <= local.end
            ]
        words = []
        for word in raw_words:
            if not isinstance(word, dict):
                raise ValueError("Invalid ASR word")
            validated = TranscriptWord.model_validate(
                {
                    "text": word.get("word", word.get("text")),
                    "start": word.get("start"),
                    "end": word.get("end"),
                }
            )
            if validated.start is None:
                raise ValueError("ASR word timestamps are absent")
            words.append(
                validated.model_copy(
                    update={
                        "start": validated.start + offset,
                        "end": validated.end + offset,
                    }
                )
            )
        start, end = local.start + offset, local.end + offset
        segments.append(
            TranscriptSegment(
                id=segment_id(index, local.text, start, end),
                text=local.text,
                start=start,
                end=end,
                words=words,
                timing_precision=("word" if words else "segment")
                if reliable_offset
                else "approximate",
            )
        )
    if not segments and payload["text"].strip():
        text = payload["text"].strip()
        segments.append(
            TranscriptSegment(
                id=segment_id(0, text, offset, offset + duration),
                text=text,
                start=offset,
                end=offset + duration,
                timing_precision="coarse" if reliable_offset else "approximate",
            )
        )
    precision = "unknown"
    if segments:
        precision = (
            "approximate"
            if not reliable_offset
            else (
                "coarse"
                if any(s.timing_precision == "coarse" for s in segments)
                else "word"
                if all(s.words for s in segments)
                else "segment"
            )
        )
    return TranscriptResult(
        segments=segments, language=payload.get("language"), timing_precision=precision
    )


class OpenRouterASR:
    def __init__(self, settings, *, transport=None):
        self.settings, self.transport = settings, transport
        parsed = urlparse(settings.openrouter_base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("OpenRouter base URL must use HTTPS")
        if not 1 <= settings.video_asr_chunk_seconds <= 30:
            raise ValueError("Audio slices must be between 1 and 30 seconds")

    async def transcribe_slice(
        self,
        path: Path,
        *,
        offset: float,
        duration: float,
        ledger: ModelUsageLedger,
        language: str | None = None,
        reliable_offset: bool = True,
    ) -> TranscriptResult:
        key = os.getenv("OPENROUTER_API_KEY")
        if not key:
            raise ValueError("Missing OpenRouter ASR credential")
        if path.stat().st_size > MAX_AUDIO_BYTES:
            raise ValueError("Audio slice exceeds API size limit")
        audio = await asyncio.to_thread(path.read_bytes)
        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    hashlib.sha256(audio).hexdigest(),
                    self.settings.video_asr_model,
                    self.settings.openrouter_base_url,
                    language,
                    offset,
                    duration,
                    reliable_offset,
                    "asr-schema-v1",
                ]
            ).encode()
        ).hexdigest()
        cached = self.settings.cloud_cache_root / "asr" / f"{fingerprint}.json"
        if cached.is_file():
            try:
                return decode_transcription(
                    json.loads(cached.read_text()),
                    offset=offset,
                    duration=duration,
                    reliable_offset=reliable_offset,
                )
            except (OSError, ValueError):
                pass
        request_id = ledger.reserve(
            "asr", self.settings.video_asr_model, duration * 0.00000333 * 1.5
        )
        payload = {
            "model": self.settings.video_asr_model,
            "input_audio": {
                "data": base64.b64encode(audio).decode("ascii"),
                "format": path.suffix.lstrip("."),
            },
            "response_format": "verbose_json",
            "timestamp_granularities": ["segment", "word"],
        }
        if language and language != "und":
            payload["language"] = language
        # No chat provider routing parameters and no automatic replay of unknown charges.
        async with httpx.AsyncClient(
            timeout=60, transport=self.transport, follow_redirects=False
        ) as client:
            response = await client.post(
                self.settings.openrouter_base_url.rstrip("/") + "/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
            )
            if response.is_error:
                ledger.settle(request_id, None)
                raise ValueError(
                    f"OpenRouter ASR HTTP {response.status_code}; charge unresolved"
                )
            if len(response.content) > 8_000_000:
                raise ValueError("ASR response is too large")
            result = response.json()
        ledger.settle(
            request_id, result.get("usage") if isinstance(result, dict) else None
        )
        decoded = decode_transcription(
            result, offset=offset, duration=duration, reliable_offset=reliable_offset
        )
        cached.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = cached.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False))
        temporary.chmod(0o600)
        temporary.replace(cached)
        return decoded

    async def transcribe(
        self, path: Path, language: str | None = None, *, control_check=None
    ) -> TranscriptResult:
        check = control_check or (lambda: None)
        check()
        if not os.getenv("OPENROUTER_API_KEY"):
            raise ValueError("Missing OpenRouter ASR credential")

        def probe():
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=start_time:format=duration",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            return json.loads(result.stdout)

        info = await asyncio.to_thread(probe)
        if not info.get("streams"):
            raise ValueError("Media has no audio track")
        duration = float(info["format"]["duration"])
        if (
            not math.isfinite(duration)
            or duration <= 0
            or duration > self.settings.max_video_duration_seconds
        ):
            raise ValueError("Invalid audio duration")
        # Normalized M4A has a known origin. Other sources retain coarse/approximate precision.
        source_start = info["streams"][0].get("start_time")
        reliable = source_start is not None and abs(float(source_start)) < 0.001
        from .media import sha256_file

        media_hash = await asyncio.to_thread(sha256_file, path)
        ledger = ModelUsageLedger(
            self.settings.cloud_cache_root / "usage" / f"{media_hash}.sqlite",
            maximum_usd=self.settings.video_cloud_max_cost_usd,
        )
        segments, detected_language = [], language
        chunk = self.settings.video_asr_chunk_seconds
        for offset in range(0, math.ceil(duration), chunk):
            check()
            slice_duration = min(chunk, duration - offset)
            output = path.parent / f"asr-slice-{offset}.wav"
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    [
                        "ffmpeg",
                        "-y",
                        "-nostdin",
                        "-loglevel",
                        "error",
                        "-ss",
                        str(offset),
                        "-i",
                        str(path),
                        "-t",
                        str(slice_duration),
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        str(output),
                    ],
                    capture_output=True,
                    check=True,
                    timeout=60,
                )
                check()
                result = await self.transcribe_slice(
                    output,
                    offset=float(offset),
                    duration=slice_duration,
                    ledger=ledger,
                    language=language,
                    reliable_offset=reliable,
                )
                check()
                segments.extend(result.segments)
                detected_language = detected_language or result.language
            finally:
                output.unlink(missing_ok=True)
        if not segments:
            raise ValueError("ASR produced no speech")
        # Rebase IDs across slices to keep them stable and globally unique.
        segments = [
            s.model_copy(update={"id": segment_id(i, s.text, s.start, s.end)})
            for i, s in enumerate(segments)
        ]
        precision = (
            "approximate"
            if not reliable
            else "coarse"
            if any(s.timing_precision == "coarse" for s in segments)
            else "word"
            if all(s.words for s in segments)
            else "segment"
        )
        return TranscriptResult(
            segments=segments, language=detected_language, timing_precision=precision
        )
