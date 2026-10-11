"""Dense, timestamped frame analysis with restartable batches and bounded requests.

Inspired by HKUDS/VideoRAG's segment_caption + merge_segment_information:
https://github.com/HKUDS/VideoRAG/blob/c412a093a820ef7a0e0dda31076ed871136198b3/VideoRAG-algorithm/videorag/_videoutil/caption.py
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace

from .model_activity import cache_hit
from .video_provider import ProviderUnavailable, provider_call_kind

DENSE_INSTRUCTION = """
Every input image is a scheduled video frame. Inspect ALL images in order,
including repeated-looking images. In addition to caption/facts/edges, return
"frame_summaries": [{"frame_index":0,"caption":"qualitative observed content"}].
Return exactly one nonempty summary for EACH supplied image index. Do not skip
indices. Use simplified Chinese for descriptions. Keep each summary concise;
exact quantities belong in supported facts, never guessed in descriptions.
Each visual fact's frame_index must identify one supplied image. The text input
contains the actual scheduled timestamps; do not invent speech or timestamps.
"""


def frame_times(start, end, interval):
    """A global, half-open sampling grid: include 0, exclude the video endpoint."""
    if (
        not all(math.isfinite(x) for x in (start, end, interval))
        or not 0 <= start < end
        or interval <= 0
    ):
        raise ValueError("Invalid frame sampling interval.")
    first = math.ceil(start / interval)
    stop = math.ceil(end / interval)
    return [round(i * interval, 9) for i in range(first, stop) if i * interval < end]


def visual_policy(video, duration, transcript_source):
    if not math.isfinite(duration) or not 0 < duration <= 4 * 3600:
        raise ValueError("Invalid video duration.")
    reason = "enabled"
    if not video.visual_enabled:
        reason = "owner_disabled"
    elif duration > video.visual_max_duration_seconds:
        reason = "duration_exceeds_visual_limit"
    interval = None
    if reason == "enabled":
        interval = (
            video.speech_frame_interval_seconds
            if transcript_source in {"local_asr", "supplied_subtitle"}
            else video.silent_frame_interval_seconds
        )
    return {
        "enabled": reason == "enabled",
        "reason": reason,
        "duration_seconds": duration,
        "max_duration_seconds": video.visual_max_duration_seconds,
        "frame_interval_seconds": interval,
        "planned_frame_count": len(frame_times(0, duration, interval))
        if interval
        else 0,
        "frames_per_request": video.visual_frames_per_request,
    }


def dense_provider_config(video, policy, segments):
    """Permit complete sampling plus bounded retries, including previous sparse usage."""
    if not policy["enabled"]:
        return video
    batches = sum(
        math.ceil(
            len(frame_times(s["start"], s["end"], policy["frame_interval_seconds"]))
            / video.visual_frames_per_request
        )
        for s in segments
        if s["start"] is not None
    )
    return replace(
        video,
        visual_request_budget=max(video.visual_request_budget, 100 + 2 * batches + 2),
        visual_image_budget=max(
            video.visual_image_budget, 100 + 2 * policy["planned_frame_count"] + 2
        ),
    )


def validate_frame_summaries(raw, times):
    values = raw.get("frame_summaries")
    if not isinstance(values, list) or len(values) != len(times):
        raise ProviderUnavailable(
            "Not every scheduled frame was described.",
            code="VISUAL_FRAME_COVERAGE_INCOMPLETE",
        )
    indexed = {}
    for value in values:
        index = value.get("frame_index") if isinstance(value, dict) else None
        caption = value.get("caption") if isinstance(value, dict) else None
        if (
            type(index) is not int
            or not 0 <= index < len(times)
            or index in indexed
            or not isinstance(caption, str)
            or not caption.strip()
        ):
            raise ProviderUnavailable(
                "Invalid frame coverage.", code="VISUAL_FRAME_COVERAGE_INCOMPLETE"
            )
        indexed[index] = {"seconds": times[index], "caption": caption.strip()[:1000]}
    return [indexed[i] for i in range(len(times))]


def request_frame_batch(provider, instruction, transcript, images, times):
    """Retry malformed/incomplete batches once with smaller image groups.

    Return separately validated parts to preserve every fact and frame index.
    Authentication/network errors are left to explicit job retry.
    """

    def request(batch, at, offset, correction=""):
        context = json.dumps(
            {"transcript": transcript, "frame_times_seconds": at}, ensure_ascii=False
        )
        with provider_call_kind(
            provider, "content_repair" if correction else "initial"
        ):
            raw = provider.json(
                instruction + DENSE_INSTRUCTION + correction, context, batch
            )
        validate_frame_summaries(raw, at)
        return {"raw": raw, "offset": offset, "count": len(batch)}

    try:
        return [request(images, times, 0)]
    except ProviderUnavailable as exc:
        if exc.code not in {
            "VISUAL_FRAME_COVERAGE_INCOMPLETE",
            "CCR_RESPONSE_TRUNCATED",
            "CCR_RESPONSE_INCOMPLETE",
            "CCR_RESPONSE_INVALID_JSON",
        }:
            raise
    correction = "\nPrevious batch was incomplete. For this smaller batch return valid JSON and exactly one frame_summaries entry for every image. Local frame_index starts at 0 for THIS batch. Keep descriptions compact, retain evidence, never skip a supplied image.\n"
    if len(images) == 1:
        return [request(images, times, 0, correction)]
    split = (len(images) + 1) // 2
    return [
        request(images[:split], times[:split], 0, correction),
        request(images[split:], times[split:], split, correction),
    ]


def analyse_frame_batches(
    video,
    provider,
    source,
    work,
    derived,
    segment,
    times,
    cache_key,
    instruction,
    *,
    ffmpeg,
    atomic_json,
    validate_extraction,
    progress=None,
    prepared_images=None,
):
    """Decode a segment once; analyse every frame in batches, checkpoint each result."""
    interval = (
        times[1] - times[0]
        if len(times) > 1
        else segment.get("frame_interval_seconds", 1.0)
    )
    images = (
        prepared_images
        if prepared_images is not None
        else decode_frames(video, source, work, segment, times, ffmpeg=ffmpeg)
    )
    if len(images) != len(times):
        raise ProviderUnavailable(
            "Prepared frame count changed.", code="VISUAL_FRAME_COVERAGE_INCOMPLETE"
        )
    result = {"caption": "", "facts": [], "edges": []}
    summaries, batches = [], []
    for offset in range(0, len(images), video.visual_frames_per_request):
        batch = images[offset : offset + video.visual_frames_per_request]
        at = times[offset : offset + len(batch)]
        batch_key = hashlib.sha256(
            (cache_key + DENSE_INSTRUCTION + json.dumps(at)).encode()
        ).hexdigest()
        checkpoint = derived / "frame-batches" / f"{segment['ordinal']}-{offset}.json"
        saved = json.loads(checkpoint.read_text()) if checkpoint.is_file() else {}
        if saved.get("cache_key") == batch_key:
            cache_hit("CCR", "frame_batch")
            parts = saved.get("parts") or [
                {"raw": saved["raw"], "offset": 0, "count": len(batch)}
            ]
        else:
            if progress:
                progress(offset, len(images))
            parts = request_frame_batch(
                provider, instruction, segment["transcript"], batch, at
            )
        frame_summaries, extracted_parts = [], []
        covered = 0
        for part in parts:
            part_offset, count = part["offset"], part["count"]
            if (
                type(part_offset) is not int
                or type(count) is not int
                or part_offset != covered
                or count <= 0
                or part_offset + count > len(batch)
            ):
                raise ProviderUnavailable(
                    "Invalid cached frame partition.",
                    code="VISUAL_FRAME_COVERAGE_INCOMPLETE",
                )
            covered += count
            part_times = at[part_offset : part_offset + count]
            frame_summaries.extend(validate_frame_summaries(part["raw"], part_times))
            extracted = validate_extraction(
                part["raw"], segment["transcript"], visual=True
            )
            for fact in extracted["facts"]:
                local_index = fact.get("frame_index")
                if local_index is not None and local_index < count:
                    fact["frame_index"] = offset + part_offset + local_index
                    fact["frame_seconds"] = part_times[local_index]
                else:
                    fact["frame_index"] = None
                if fact["evidence_kind"] == "visual":
                    fact["observed_interval"] = [
                        part_times[0],
                        min(segment["end"], part_times[-1] + interval),
                    ]
            extracted_parts.append(extracted)
        if covered != len(batch):
            raise ProviderUnavailable(
                "Cached frame partition is incomplete.",
                code="VISUAL_FRAME_COVERAGE_INCOMPLETE",
            )
        if saved.get("cache_key") != batch_key:
            atomic_json(
                checkpoint,
                {
                    "cache_key": batch_key,
                    **(
                        {"raw": parts[0]["raw"]}
                        if len(parts) == 1
                        else {"parts": parts}
                    ),
                },
            )
        for extracted in extracted_parts:
            result["facts"].extend(extracted["facts"])
            result["edges"].extend(extracted["edges"])
        summaries.extend(frame_summaries)
        batches.append(
            {
                "start_seconds": at[0],
                "end_seconds": min(segment["end"], at[-1] + interval),
                "caption": "\n".join(
                    part["caption"] for part in extracted_parts if part["caption"]
                ),
                "frame_count": len(batch),
            }
        )
    result["caption"] = "\n".join(
        f"[{s['seconds']:g}s] {s['caption']}" for s in summaries
    )
    segment["frame_summaries"], segment["visual_batches"] = summaries, batches
    segment["analysed_frame_count"] = len(summaries)
    return result, images


def embed_complete_text(encoder, text, chunk_size=3000):
    """Pool local document vectors without silently truncating dense visual knowledge."""
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("Embedding chunk size must be a positive integer.")
    byte_limit = None
    limit_reader = getattr(encoder, "document_byte_limit", None)
    if callable(limit_reader):
        byte_limit = limit_reader()
        if type(byte_limit) is not int or byte_limit <= 0:
            raise ValueError("Invalid document byte limit.")
    chunks = []
    start, byte_count = 0, 0
    for index, character in enumerate(text):
        size = len(character.encode("utf-8"))
        if byte_limit is not None and size > byte_limit:
            raise ValueError("Document byte limit cannot fit one Unicode character.")
        if index - start >= chunk_size or (
            byte_limit is not None and byte_count + size > byte_limit
        ):
            chunks.append(text[start:index])
            start, byte_count = index, 0
        byte_count += size
    if start < len(text):
        chunks.append(text[start:])
    vectors = [list(encoder.embed_document(chunk)) for chunk in chunks]
    if not vectors:
        raise ValueError("No text to embed.")
    if len(vectors) == 1:
        return vectors[0]
    if any(len(v) != len(vectors[0]) for v in vectors):
        raise ValueError("Mismatched local embedding dimensions.")
    pooled = [sum(v[i] for v in vectors) / len(vectors) for i in range(len(vectors[0]))]
    norm = math.sqrt(sum(x * x for x in pooled))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("Invalid pooled local embedding.")
    return [x / norm for x in pooled]


def decode_frames(video, source, work, segment, times, *, ffmpeg):
    frame_root = work / f"frames-{segment['ordinal']}"
    frame_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    interval = (
        times[1] - times[0]
        if len(times) > 1
        else segment.get("frame_interval_seconds", 1.0)
    )
    ffmpeg(
        video,
        [
            "-ss",
            str(times[0]),
            "-t",
            str(segment["end"] - times[0]),
            "-i",
            str(source),
            "-an",
            "-vf",
            f"tpad=stop_mode=clone:stop_duration={interval},fps={1 / interval}:start_time=0:round=up,scale=960:-2",
            "-frames:v",
            str(len(times)),
            "-start_number",
            "0",
            "-q:v",
            "2",
            str(frame_root / "%06d.jpg"),
        ],
    )
    images = [frame_root / f"{i:06d}.jpg" for i in range(len(times))]
    if not all(path.is_file() and path.stat().st_size for path in images):
        raise ProviderUnavailable(
            "Scheduled frames were not fully decoded.",
            code="VISUAL_FRAME_EXTRACTION_INCOMPLETE",
        )
    return images
