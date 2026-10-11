"""Recheck saved ASR facts using retained knowledge, without reading media."""

from __future__ import annotations

import json
from uuid import UUID

from pgvector import Vector
from psycopg.types.json import Jsonb

from .config import ConfigError
from .database import connect_database
from .embedding import client_from_config, embedding_space_id
from .indexes import resolve_index
from .video_store import library_id
from .video_worker import atomic_json, crosscheck_asr_quantities


def recheck_asr(config, video, job_id: str):
    job_id = str(UUID(job_id))
    with connect_database(config) as c:
        job = c.execute(
            "SELECT payload,state FROM public.rag_video_jobs WHERE id=%s AND library_id=%s",
            (job_id, library_id(video)),
        ).fetchone()
    if (
        not job
        or job[1] != "complete"
        or job[0].get("transcript_asset_id")
        or not job[0].get("asset_id")
    ):
        raise ConfigError("Recheck requires a completed local-ASR video job.")
    derived = video.root / "derived" / job_id
    record_path = derived / "record.json"
    record = json.loads(record_path.read_text())
    if record["id"] != job_id:
        raise ConfigError("Knowledge checkpoint does not match the job.")
    backup = derived / "record-before-asr-recheck.json"
    if not backup.exists():
        atomic_json(backup, record)
    profile = resolve_index(config)
    space = embedding_space_id(profile.embedding)
    client = client_from_config(profile)
    changes = []
    for segment in record["segments"]:
        before = json.dumps(segment["facts"], sort_keys=True)
        crosscheck_asr_quantities(segment)
        if before == json.dumps(segment["facts"], sort_keys=True):
            continue
        if segment["text_space_id"] != space:
            raise ConfigError(
                "Use the matching text encoder to recheck this saved segment."
            )
        text = "\n".join(
            [
                segment["transcript"],
                segment["caption"],
                *[
                    f["statement"]
                    + (" [unknown]" if f["uncertain"] else "")
                    + (" [conflict]" if f["conflict"] else "")
                    for f in segment["facts"]
                ],
            ]
        )
        segment["text_embedding"] = list(client.embed_document(text))
        changes.append(segment)
    record["transcript_source"] = "local_asr"
    record["asr_quantity_policy"] = "requires_visual_corroboration"
    atomic_json(record_path, record)
    if changes:
        with connect_database(profile) as c:
            for segment in changes:
                c.execute(
                    "UPDATE public.rag_video_segments SET facts=%s,text_embedding=%s WHERE id=%s AND video_id=%s AND text_space_id=%s",
                    (
                        Jsonb(segment["facts"]),
                        Vector(segment["text_embedding"]),
                        segment["id"],
                        job_id,
                        space,
                    ),
                )
        correction = [
            "# ASR 数值复核记录",
            "",
            "自动转录中的数值仅在视觉事实确认后作为确定值；原始记录保留用于审查。",
            "",
        ]
        for s in record["segments"]:
            correction += [
                f"## Segment {s['ordinal']}",
                *[
                    "- "
                    + f["statement"]
                    + (" [unknown]" if f["uncertain"] else "")
                    + (" [conflict]" if f["conflict"] else "")
                    for f in s["facts"]
                ],
            ]
        (derived / "knowledge-rechecked.md").write_text(
            "\n".join(correction), encoding="utf-8"
        )
    return {
        "job_id": job_id,
        "changed_segments": len(changes),
        "original_media_reopened": False,
    }
