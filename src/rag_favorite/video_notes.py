"""Index author-written note text independently of video transcription."""

from __future__ import annotations

import json
from uuid import UUID

from .config import ConfigError
from .database import connect_database
from .rag import ingest_file
from .video_store import library_id
from .xhs_private import canonical_note_url


def index_author_note(config, video, job_id):
    job_id = str(UUID(job_id))
    with connect_database(config) as c:
        row = c.execute(
            "SELECT collection,payload FROM public.rag_video_jobs WHERE id=%s AND library_id=%s",
            (job_id, library_id(video)),
        ).fetchone()
    if not row:
        raise ConfigError("Unknown ingestion job.")
    collection, payload = config.collection(row[0]), row[1]
    if collection.read_only:
        raise ConfigError("Selected collection is read-only.")
    source = video.root / "derived" / job_id / "source.json"
    if not source.is_file():
        raise ConfigError("Saved source note is unavailable.")
    note = json.loads(source.read_text())
    if not note.get("description", "").strip():
        return {"job_id": job_id, "author_note_indexed": False}
    generated = collection.path / ".generated"
    document = generated / ("xhs-" + job_id + ".md")
    if generated.is_symlink() or document.is_symlink():
        raise ConfigError("Unsafe generated document path.")
    generated.mkdir(parents=True, exist_ok=True)
    if not document.exists():
        with document.open("x", encoding="utf-8") as f:
            f.write(
                "# "
                + note["title"]
                + "\n\n"
                + "来源：作者笔记正文；没有视频时间信息。\n\n"
                + "Source: "
                + canonical_note_url(payload["source_url"])
                + "\n\n"
                + note["description"]
                + "\n"
            )
    ingest_file(document, collection.key, config)
    with connect_database(config) as c:
        c.execute(
            "UPDATE public.rag_video_jobs SET payload=jsonb_set(payload,'{author_note_indexed}','true') WHERE id=%s AND library_id=%s",
            (job_id, library_id(video)),
        )
    return {"job_id": job_id, "author_note_indexed": True, "video_state_changed": False}
