"""Provider-independent transcript times, stable IDs and Markdown compatibility."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Precision = Literal["word", "segment", "coarse", "approximate", "unknown"]


class TranscriptWord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    start: float | None = None
    end: float | None = None

    @model_validator(mode="after")
    def validate_interval(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("Both timestamp bounds must be present or absent")
        if self.start is not None and (
            not math.isfinite(self.start)
            or not math.isfinite(self.end)
            or self.start < 0
            or self.end < self.start
        ):
            raise ValueError("Invalid transcript interval")
        return self


class TranscriptSegment(TranscriptWord):
    id: str
    words: list[TranscriptWord] = Field(default_factory=list)
    timing_precision: Precision = "segment"

    @model_validator(mode="after")
    def validate_words(self):
        if self.start is None and self.timing_precision != "unknown":
            raise ValueError("Missing timestamps must have unknown precision")
        for word in self.words:
            if (
                word.start is None
                or self.start is None
                or word.start < self.start
                or word.end > self.end
            ):
                raise ValueError("Word lies outside its segment")
        return self


class TranscriptResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segments: list[TranscriptSegment] = Field(default_factory=list)
    language: str | None = None
    language_probability: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )
    timing_precision: Precision = "unknown"

    @model_validator(mode="after")
    def validate_ids(self):
        if len({s.id for s in self.segments}) != len(self.segments):
            raise ValueError("Transcript segment IDs must be unique")
        return self

    @property
    def text(self) -> str:
        return "\n".join(segment.text for segment in self.segments)

    @property
    def checksum(self) -> str:
        payload = [s.model_dump(mode="json") for s in self.segments]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()

    def to_markdown(self) -> str:
        lines = [
            "# Video Transcription",
            "",
            f"**Detected Language:** {self.language or 'und'}",
            "",
            "## Transcription Content",
            "",
        ]
        if self.language_probability is not None:
            lines.insert(
                3, f"**Language Probability:** {self.language_probability:.2f}"
            )
        for segment in self.segments:
            if segment.start is not None:
                lines.extend(
                    [
                        f"**[{format_time(segment.start)} - {format_time(segment.end)}]**",
                        "",
                    ]
                )
                # A compatibility string must not silently upgrade coarse timing.
                if segment.timing_precision in {"coarse", "approximate"}:
                    lines.extend(
                        [f"<!-- timing_precision:{segment.timing_precision} -->", ""]
                    )
            lines.extend([segment.text, ""])
        return "\n".join(lines)


def segment_id(index: int, text: str, start: float | None, end: float | None) -> str:
    payload = json.dumps([index, text, start, end], ensure_ascii=False)
    return f"seg-{hashlib.sha256(payload.encode()).hexdigest()[:16]}"


def parse_time(value: str) -> float:
    parts = value.strip().replace(",", ".").split(":")
    if len(parts) not in {2, 3}:
        raise ValueError("Timestamp must be MM:SS or HH:MM:SS")
    numbers = [float(part) for part in parts]
    if any(not math.isfinite(v) or v < 0 for v in numbers) or numbers[-1] >= 60:
        raise ValueError("Invalid timestamp")
    if len(parts) == 3 and numbers[1] >= 60:
        raise ValueError("Invalid minute field")
    return sum(v * 60**i for i, v in enumerate(reversed(numbers)))


def format_time(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Invalid timestamp")
    millis = round(seconds * 1000)
    minutes, remainder = divmod(millis, 60000)
    whole, fraction = divmod(remainder, 1000)
    return f"{minutes:02d}:{whole:02d}" + (f".{fraction:03d}" if fraction else "")


def from_markdown(value: str, language: str | None = None) -> TranscriptResult:
    from .markdown import transcript_body

    body = transcript_body(value)
    pattern = re.compile(r"\*\*\[([\d:.,]+)\s*-\s*([\d:.,]+)\]\*\*")
    matches = list(pattern.finditer(body))
    entries = []
    if matches and body[: matches[0].start()].strip():
        entries.append((None, None, body[: matches[0].start()].strip(), "unknown"))
    for i, match in enumerate(matches):
        text = body[
            match.end() : matches[i + 1].start() if i + 1 < len(matches) else len(body)
        ].strip()
        marker = re.search(r"<!-- timing_precision:(coarse|approximate) -->", text)
        precision = marker.group(1) if marker else "segment"
        text = re.sub(
            r"<!-- timing_precision:(coarse|approximate) -->\s*", "", text
        ).strip()
        if text:
            entries.append(
                (parse_time(match[1]), parse_time(match[2]), text, precision)
            )
    if not matches and body:
        entries.append((None, None, body, "unknown"))
    segments = [
        TranscriptSegment(
            id=segment_id(i, text, start, end),
            text=text,
            start=start,
            end=end,
            timing_precision=precision,
        )
        for i, (start, end, text, precision) in enumerate(entries)
    ]
    if not language:
        match = re.search(r"\*\*Detected Language:\*\*\s*([^\n]+)", value)
        language = match[1].strip() if match else None
    precisions = {s.timing_precision for s in segments}
    precision = next(iter(precisions)) if len(precisions) == 1 else "unknown"
    return TranscriptResult(
        segments=segments, language=language, timing_precision=precision
    )


def parse_subtitles(content: str) -> list[dict]:
    """Deduplicate only adjacent overlapping rolling cues, retaining later repeats."""
    import html

    result = []
    for block in re.split(r"\n\s*\n", content.replace("\r\n", "\n").strip()):
        lines = block.splitlines()
        for index, line in enumerate(lines):
            match = re.match(r"([\d:.,]+)\s*-->\s*([\d:.,]+)", line.strip())
            if not match:
                continue
            start, end = parse_time(match[1]), parse_time(match[2])
            text = re.sub(
                r"\s+",
                " ",
                html.unescape(re.sub(r"<[^>]+>", "", " ".join(lines[index + 1 :]))),
            ).strip()
            TranscriptWord(text=text, start=start, end=end)
            if not text:
                break
            entry = {"start": start, "end": end, "text": text}
            if result and start <= result[-1]["end"] and start >= result[-1]["start"]:
                previous = result[-1]
                if text == previous["text"] or text.startswith(previous["text"]):
                    entry["start"] = previous["start"]
                    entry["end"] = max(end, previous["end"])
                    result[-1] = entry
                    break
            result.append(entry)
            break
    return result
