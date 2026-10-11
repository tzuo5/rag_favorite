"""Subtitle-first transcript parsing, without inventing timestamps."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path


def seconds(value: str) -> float:
    parts = value.strip().replace(",", ".").split(":")
    if len(parts) not in {2, 3}:
        raise ValueError("Invalid subtitle timestamp.")
    values = [float(p) for p in parts]
    if (
        any(not math.isfinite(v) or v < 0 for v in values)
        or values[-1] >= 60
        or (len(values) == 3 and values[1] >= 60)
    ):
        raise ValueError("Invalid subtitle timestamp.")
    return sum(v * 60**i for i, v in enumerate(reversed(values)))


def parse_transcript(path: Path, suffix: str) -> list[dict]:
    if path.stat().st_size > 5_000_000:
        raise ValueError("Transcript asset is too large.")
    text = path.read_text(encoding="utf-8-sig")
    if suffix == ".json":
        raw = json.loads(text)
        segments = raw.get("segments", []) if isinstance(raw, dict) else raw
    elif suffix in {".srt", ".vtt"}:
        segments = []
        lines = text.replace("\r\n", "\n").splitlines()
        for index, line in enumerate(lines):
            if " --> " not in line:
                continue
            start, end = line.split(" --> ", 1)
            content = []
            for next_line in lines[index + 1 :]:
                if not next_line.strip():
                    break
                content.append(re.sub(r"<[^>]+>", "", next_line))
            segments.append(
                {
                    "start": seconds(start),
                    "end": seconds(end.split()[0]),
                    "text": " ".join(content),
                    "timing_precision": "segment",
                }
            )
    else:
        segments = [
            {
                "start": None,
                "end": None,
                "text": text.strip(),
                "timing_precision": "unknown",
            }
        ]
    result = []
    for item in segments:
        start, end = item.get("start"), item.get("end")
        if (start is None) != (end is None) or (
            start is not None
            and (
                not math.isfinite(start)
                or not math.isfinite(end)
                or start < 0
                or end < start
            )
        ):
            raise ValueError("Invalid transcript interval.")
        content = str(item.get("text", "")).strip()
        if content:
            item_result = {
                "start": start,
                "end": end,
                "text": content,
                "timing_precision": "unknown"
                if start is None
                else item.get("timing_precision", "segment"),
            }
            words = item.get("words") or []
            for word in words:
                if (
                    start is None
                    or not isinstance(word, dict)
                    or not isinstance(word.get("start"), (int, float))
                    or not isinstance(word.get("end"), (int, float))
                    or not math.isfinite(word["start"])
                    or not math.isfinite(word["end"])
                    or word["start"] < start
                    or word["end"] > end
                    or word["end"] < word["start"]
                ):
                    raise ValueError(
                        "Word timestamp lies outside its transcript segment."
                    )
            if words:
                item_result["words"] = words
            result.append(item_result)
    if not result:
        raise ValueError("Transcript is empty.")
    return result


def partition_transcript(
    transcript: list[dict], duration: float, length: int
) -> list[dict]:
    if not math.isfinite(duration) or not 0 < duration <= 4 * 3600:
        raise ValueError("Invalid or excessively long video duration.")
    # Untimed text remains explicitly untimed; it cannot support visual alignment.
    if any(s["start"] is None for s in transcript):
        return [
            {
                "ordinal": 0,
                "start": None,
                "end": None,
                "timing_precision": "unknown",
                "transcript": "\n".join(s["text"] for s in transcript),
            }
        ]
    if any(s["end"] > duration + 1 for s in transcript):
        raise ValueError("Transcript exceeds the video duration.")
    result = []
    for index in range(math.ceil(duration / length)):
        start, end = index * length, min((index + 1) * length, duration)
        texts = [
            s["text"] for s in transcript if s["start"] < end and s["end"] >= start
        ]
        result.append(
            {
                "ordinal": index,
                "start": start,
                "end": end,
                "timing_precision": "segment",
                "transcript": "\n".join(texts),
            }
        )
    return result
