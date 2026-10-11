"""Durable jobs and derived knowledge in the existing PostgreSQL instance."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from uuid import UUID, uuid4

from pgvector import Vector
from psycopg.types.json import Jsonb

from .config import AppConfig, ConfigError
from .database import connect_database
from .video_config import VideoConfig

ASSET_ID = re.compile(r"^[a-f0-9]{32}$")


def _title_duplicate(connection, config, video, title, details, *, migration_id=None):
    if not getattr(config, "unified", False):
        return None
    from .title_dedup import find_title_job, retain_submission

    found = find_title_job(connection, video, title, migration_id=migration_id)
    if not found:
        return None
    retain_submission(connection, video, found[0], details)
    return {
        "job_id": str(found[0]),
        "state": found[1],
        "duplicate": True,
        "dedup_reason": "title",
    }


def _duplicate_result(config, video, duplicate, connection=None):
    if duplicate[1] == "duplicate" and getattr(config, "unified", False):
        from .title_dedup import canonical_job

        if connection is None:
            with connect_database(config) as c:
                return _duplicate_result(config, video, duplicate, c)
        ident = canonical_job(connection, video, duplicate[0])
        row = connection.execute(
            "SELECT state FROM public.rag_video_jobs WHERE id=%s AND library_id=%s",
            (ident, library_id(video)),
        ).fetchone()
        return {
            "job_id": ident,
            "state": row[0],
            "duplicate": True,
            "dedup_reason": "title",
        }
    return {"job_id": str(duplicate[0]), "state": duplicate[1], "duplicate": True}


def library_id(video: VideoConfig) -> str:
    return hashlib.sha256(str(video.root.resolve()).encode()).hexdigest()


def ingestion_collection(config, video, requested="all"):
    """One default storage target; legacy explicit collections remain compatible."""
    if getattr(config, "unified", False):
        return config.collection(requested or "all").key
    key = video.default_collection if requested in {None, "all"} else requested
    return config.collection(key).key


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def stage_asset(source: Path, video: VideoConfig) -> dict:
    source = source.expanduser().resolve()
    if not source.is_file() or source.stat().st_size > video.max_asset_bytes:
        raise ConfigError("Asset is missing or exceeds the configured size limit.")
    if source.suffix.lower() not in {
        ".mp4",
        ".mkv",
        ".mov",
        ".webm",
        ".srt",
        ".vtt",
        ".txt",
        ".json",
    }:
        raise ConfigError("Unsupported video or transcript asset type.")
    asset_id = uuid4().hex
    root = video.root / "assets"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / asset_id
    before = digest_file(source)
    with source.open("rb") as src, target.open("xb") as dst:
        shutil.copyfileobj(src, dst)
    after = digest_file(target)
    if after != before or digest_file(source) != before:
        target.unlink()
        raise ConfigError("Asset changed while copying; retry.")
    manifest = {
        "asset_id": asset_id,
        "name": source.name,
        "suffix": source.suffix.lower(),
        "sha256": after,
        "bytes": target.stat().st_size,
    }
    (root / (asset_id + ".json")).write_text(json.dumps(manifest, ensure_ascii=False))
    target.chmod(0o600)
    return manifest


def asset_path(video: VideoConfig, asset_id: str) -> tuple[Path, dict]:
    if not ASSET_ID.fullmatch(asset_id):
        raise ConfigError("Invalid asset ID.")
    root = (video.root / "assets").resolve()
    path = root / asset_id
    if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
        raise ConfigError("Staged asset is unavailable.")
    manifest = json.loads((root / (asset_id + ".json")).read_text())
    if digest_file(path) != manifest["sha256"]:
        raise ConfigError("Staged asset checksum mismatch.")
    return path, manifest


def enqueue(
    config: AppConfig,
    video: VideoConfig,
    asset_id: str,
    collection: str,
    transcript_asset_id: str | None = None,
    title: str = "",
    *,
    priority: int = 10,
    submission_key: str | None = None,
) -> dict:
    collection = ingestion_collection(config, video, collection)
    selected = config.collection(collection)
    if selected.read_only:
        raise ConfigError("Selected collection is read-only.")
    if not ASSET_ID.fullmatch(asset_id):
        raise ConfigError("Invalid asset ID.")
    with connect_database(config) as c:
        duplicate = c.execute(
            "SELECT id,state,collection FROM public.rag_video_jobs WHERE library_id=%s AND (payload->>'asset_id'=%s OR (%s::text IS NOT NULL AND payload->>'submission_key'=%s)) LIMIT 1",
            (library_id(video), asset_id, submission_key, submission_key),
        ).fetchone()
    if duplicate:
        if duplicate[2] != collection and not getattr(config, "unified", False):
            raise ConfigError("This asset was submitted to a different collection.")
        return _duplicate_result(config, video, duplicate)
    _, manifest = asset_path(video, asset_id)
    if manifest["suffix"] not in {".mp4", ".mkv", ".mov", ".webm"}:
        raise ConfigError("The video asset must contain video media.")
    if transcript_asset_id:
        _, transcript = asset_path(video, transcript_asset_id)
        if transcript["suffix"] not in {".srt", ".vtt", ".txt", ".json"}:
            raise ConfigError("Invalid transcript asset type.")
    job_id = str(uuid4())
    payload = {
        "asset_id": asset_id,
        "transcript_asset_id": transcript_asset_id,
        "title": title[:300] or Path(manifest["name"]).stem,
        "source_label": manifest["name"],
        "sha256": manifest["sha256"],
    }
    if submission_key:
        payload["submission_key"] = submission_key
    with connect_database(config) as c:
        # Serialize submissions so one owned source cannot be consumed by two jobs.
        c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (library_id(video),))
        duplicate = c.execute(
            "SELECT id,state,collection FROM public.rag_video_jobs WHERE library_id=%s AND (payload->>'asset_id'=%s OR (%s::text IS NOT NULL AND payload->>'submission_key'=%s)) LIMIT 1",
            (library_id(video), asset_id, submission_key, submission_key),
        ).fetchone()
        if duplicate:
            if duplicate[2] != collection and not getattr(config, "unified", False):
                raise ConfigError("This asset was submitted to a different collection.")
            return _duplicate_result(config, video, duplicate, c)
        duplicate = _title_duplicate(
            c,
            config,
            video,
            payload["title"],
            {"asset_id": asset_id, "source_label": manifest["name"]},
        )
        if duplicate:
            return duplicate
        from .title_dedup import title_text

        c.execute(
            "INSERT INTO public.rag_video_jobs(id,library_id,collection,state,payload,priority,title_key) VALUES(%s,%s,%s,'queued',%s,%s,%s)",
            (
                job_id,
                library_id(video),
                collection,
                Jsonb(payload),
                priority,
                title_text(payload["title"]),
            ),
        )
    return {"job_id": job_id, "state": "queued"}


def enqueue_url(
    config,
    video,
    source_url: str,
    collection: str,
    title: str = "",
    *,
    source_note_id: str | None = None,
    allow_expired: bool = True,
    source_origin: str = "manual",
    source_author_id: str = "",
    source_page_cursor: str = "",
):
    from urllib.parse import urlsplit

    from .video_sources import validate_source_url

    if urlsplit(source_url).hostname in {
        "www.youtube.com",
        "youtube.com",
        "youtu.be",
        "www.bilibili.com",
        "bilibili.com",
    }:
        return enqueue_external_source(config, video, source_url, collection, title)
    canonical_id = re.fullmatch(
        r"/(?:explore|discovery/item)/([a-f0-9]{24})", urlsplit(source_url).path
    )
    if canonical_id:
        if source_note_id and source_note_id != canonical_id[1]:
            raise ConfigError("Source URL and note ID disagree.")
        source_note_id = canonical_id[1]

    collection = ingestion_collection(config, video, collection)
    validate_source_url(source_url)
    if source_note_id is not None and not re.fullmatch(r"[a-f0-9]{24}", source_note_id):
        raise ConfigError("Invalid source note ID.")
    if source_origin not in {"manual", "favorites", "following", "live_photo"}:
        raise ConfigError("Invalid source origin.")
    if source_author_id and not re.fullmatch(r"[a-f0-9]{24}", source_author_id):
        raise ConfigError("Invalid source author ID.")
    if not isinstance(source_page_cursor, str) or len(source_page_cursor) > 4000:
        raise ConfigError("Invalid source cursor.")
    if config.collection(collection).read_only:
        raise ConfigError("Selected collection is read-only.")
    with connect_database(config) as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (library_id(video),))
        duplicate_title = _title_duplicate(
            c,
            config,
            video,
            title,
            {
                "source_url": source_url,
                "source_note_id": source_note_id,
                "origin": source_origin,
            },
        )
        if duplicate_title:
            if source_note_id:
                record_source(
                    c,
                    video,
                    collection,
                    source_note_id,
                    source_origin,
                    source_author_id,
                    duplicate_title["job_id"],
                )
            return duplicate_title
        if source_note_id:
            # Earlier share-link jobs predate note-ID payloads. Match their retained metadata.
            legacy = c.execute(
                "SELECT id FROM public.rag_video_jobs WHERE library_id=%s AND (collection=%s OR %s) AND payload ? 'source_url' AND NOT payload ? 'source_note_id'",
                (library_id(video), collection, getattr(config, "unified", False)),
            ).fetchall()
            for (previous_id,) in legacy:
                metadata_path = (
                    video.root / "derived" / str(previous_id) / "source.json"
                )
                if metadata_path.is_file():
                    try:
                        metadata = json.loads(metadata_path.read_text())
                    except (OSError, ValueError):
                        continue
                    if not isinstance(metadata, dict):
                        continue
                    if metadata.get("note_id") == source_note_id:
                        c.execute(
                            "UPDATE public.rag_video_jobs SET payload=payload || %s WHERE id=%s AND library_id=%s",
                            (
                                Jsonb({"source_note_id": source_note_id}),
                                previous_id,
                                library_id(video),
                            ),
                        )
        duplicate = c.execute(
            "SELECT id,state FROM public.rag_video_jobs WHERE library_id=%s AND (collection=%s OR %s) AND (payload->>'source_url'=%s OR (%s::text IS NOT NULL AND (payload->>'source_note_id'=%s OR split_part(payload->>'source_url','?',1) IN (%s,%s)))) AND (%s OR payload->>'media_expired' IS DISTINCT FROM 'true') ORDER BY created_at DESC LIMIT 1",
            (
                library_id(video),
                collection,
                getattr(config, "unified", False),
                source_url,
                source_note_id,
                source_note_id,
                "https://www.xiaohongshu.com/explore/" + source_note_id
                if source_note_id
                else None,
                "https://www.xiaohongshu.com/discovery/item/" + source_note_id
                if source_note_id
                else None,
                not allow_expired,
            ),
        ).fetchone()
        if duplicate:
            if duplicate[1] == "duplicate" and getattr(config, "unified", False):
                result = _duplicate_result(config, video, duplicate, c)
                if source_note_id:
                    record_source(
                        c,
                        video,
                        collection,
                        source_note_id,
                        source_origin,
                        source_author_id,
                        result["job_id"],
                    )
                return result
            if source_origin == "following" and duplicate[1] == "complete":
                upgraded = c.execute(
                    "UPDATE public.rag_video_jobs SET state='queued',stage='queued',error_code=NULL,updated_at=now() WHERE id=%s AND library_id=%s AND state='complete' AND stage='text_only' AND payload->>'image_note_version' IS DISTINCT FROM '1' RETURNING id",
                    (duplicate[0], library_id(video)),
                ).fetchone()
                if upgraded:
                    duplicate = (duplicate[0], "queued")
            # Keep pending jobs' access tokens fresh without changing running/completed work.
            if duplicate[1] == "queued":
                c.execute(
                    "UPDATE public.rag_video_jobs SET payload=payload || %s WHERE id=%s AND library_id=%s AND state='queued'",
                    (
                        Jsonb(
                            {
                                "source_url": source_url,
                                "source_note_id": source_note_id,
                                **(
                                    {
                                        "source_author_id": source_author_id,
                                        "source_page_cursor": source_page_cursor,
                                    }
                                    if source_author_id
                                    else {}
                                ),
                            }
                        )
                        if source_note_id
                        else Jsonb({"source_url": source_url}),
                        duplicate[0],
                        library_id(video),
                    ),
                )
            if source_note_id:
                record_source(
                    c,
                    video,
                    collection,
                    source_note_id,
                    source_origin,
                    source_author_id,
                    duplicate[0],
                )
            return {
                "job_id": str(duplicate[0]),
                "state": duplicate[1],
                "duplicate": True,
            }
        job_id = str(uuid4())
        from .title_dedup import title_text

        c.execute(
            "INSERT INTO public.rag_video_jobs(id,library_id,collection,state,payload,priority,title_key) VALUES(%s,%s,%s,'queued',%s,10,%s)",
            (
                job_id,
                library_id(video),
                collection,
                Jsonb(
                    {
                        "source_url": source_url,
                        "title": title[:300],
                        **(
                            {
                                "source_author_id": source_author_id,
                                "source_page_cursor": source_page_cursor,
                            }
                            if source_author_id
                            else {}
                        ),
                        **(
                            {"source_note_id": source_note_id} if source_note_id else {}
                        ),
                    }
                ),
                title_text(title),
            ),
        )
        if source_note_id:
            record_source(
                c,
                video,
                collection,
                source_note_id,
                source_origin,
                source_author_id,
                job_id,
            )
    return {"job_id": job_id, "state": "queued"}


def record_source(
    connection, video, collection, note_id, origin, author_id, job_id, component=""
):
    connection.execute(
        "INSERT INTO public.rag_xhs_sources(library_id,collection,note_id,origin,author_id,job_id,component) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(library_id,collection,note_id,origin,author_id,component) DO UPDATE SET job_id=EXCLUDED.job_id",
        (library_id(video), collection, note_id, origin, author_id, job_id, component),
    )


def job_status(config: AppConfig, video: VideoConfig, job_id: str) -> dict:
    job_id = str(UUID(job_id))
    with connect_database(config) as c:
        row = c.execute(
            "SELECT collection,state,stage,error_code,attempts,created_at,updated_at FROM public.rag_video_jobs WHERE id=%s AND library_id=%s",
            (job_id, library_id(video)),
        ).fetchone()
    if not row:
        raise ConfigError("Unknown ingestion job.")
    return dict(
        zip(
            (
                "collection",
                "state",
                "stage",
                "error_code",
                "attempts",
                "created_at",
                "updated_at",
            ),
            [*row[:5], str(row[5]), str(row[6])],
            strict=True,
        )
    ) | {"job_id": job_id}


def update_job(
    config: AppConfig,
    video: VideoConfig,
    job_id: str,
    state: str,
    stage: str,
    error_code=None,
):
    from .pipeline_fence import assert_owner

    with connect_database(config) as c:
        assert_owner(c)
        c.execute(
            "UPDATE public.rag_video_jobs SET state=%s,stage=%s,error_code=%s,updated_at=now() WHERE id=%s AND library_id=%s",
            (state, stage, error_code, job_id, library_id(video)),
        )


def publish(
    config: AppConfig, video: VideoConfig, job: dict, record: dict, segments: list[dict]
):
    from .pipeline_fence import assert_owner

    with connect_database(config) as c:
        assert_owner(c)
        c.execute(
            "INSERT INTO public.rag_videos(id,library_id,collection,title,source_label,content_sha256,classification,transcript,provider_usage) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING",
            (
                job["id"],
                library_id(video),
                job["collection"],
                job["payload"]["title"],
                job["payload"]["source_label"],
                job["payload"]["sha256"],
                Jsonb(record["classification"]),
                Jsonb(record["transcript"]),
                Jsonb(record["usage"]),
            ),
        )
        for segment in segments:
            c.execute(
                """INSERT INTO public.rag_video_segments(id,video_id,ordinal,start_seconds,end_seconds,timing_precision,transcript,caption,facts,evidence,text_space_id,text_embedding,video_space_id,video_embedding)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
                (
                    segment["id"],
                    job["id"],
                    segment["ordinal"],
                    segment["start"],
                    segment["end"],
                    segment["timing_precision"],
                    segment["transcript"],
                    segment["caption"],
                    Jsonb(segment["facts"]),
                    Jsonb(segment["evidence"]),
                    segment.get("text_space_id"),
                    Vector(segment["text_embedding"])
                    if segment.get("text_embedding")
                    else None,
                    segment.get("video_space_id"),
                    Vector(segment["video_embedding"])
                    if segment.get("video_embedding")
                    else None,
                ),
            )
            for edge in segment.get("edges", []):
                c.execute(
                    "INSERT INTO public.rag_video_edges(segment_id,subject,relation,object,provenance) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        segment["id"],
                        edge["subject"],
                        edge["relation"],
                        edge["object"],
                        Jsonb(
                            {
                                "segment_id": segment["id"],
                                "evidence_kind": edge.get(
                                    "evidence_kind", "transcript"
                                ),
                            }
                        ),
                    ),
                )
        c.execute(
            "UPDATE public.rag_video_jobs SET state='published',stage='cleanup',updated_at=now() WHERE id=%s",
            (job["id"],),
        )


def finish_cleanup(config: AppConfig, video: VideoConfig, job_id: str):
    from .pipeline_fence import assert_owner

    with connect_database(config) as c:
        assert_owner(c)
        c.execute(
            "UPDATE public.rag_videos SET source_deleted=true WHERE id=%s AND library_id=%s",
            (job_id, library_id(video)),
        )
        c.execute(
            "UPDATE public.rag_video_jobs SET state='complete',stage='complete',error_code=NULL,updated_at=now() WHERE id=%s AND library_id=%s",
            (job_id, library_id(video)),
        )


def enqueue_reprocess(config, video, source_url, migration_id, document_ids):
    """Exactly one fresh source task per migration; never reset a completed task."""
    from .video_sources import normalize_media_url

    source_key, canonical = normalize_media_url(source_url)
    collection = ingestion_collection(config, video)
    lid = library_id(video)
    with connect_database(config) as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lid,))
        existing = c.execute(
            "SELECT job_id FROM public.rag_legacy_reprocess WHERE library_id=%s AND migration_id=%s AND source_key=%s",
            (lid, migration_id, source_key),
        ).fetchone()
        if existing:
            row = c.execute(
                "SELECT id,state FROM public.rag_video_jobs WHERE id=%s", (existing[0],)
            ).fetchone()
            return _duplicate_result(config, video, row, c)
        from .title_dedup import title_text

        rows = c.execute(
            "SELECT title,metadata FROM public.rag_knowledge_documents WHERE library_id=%s AND id::text=ANY(%s::text[])",
            (lid, document_ids),
        ).fetchall()
        title = next(
            (
                title_text(t, legacy_filename=bool(meta.get("source_paths")))
                for t, meta in rows
                if title_text(t, legacy_filename=bool(meta.get("source_paths")))
            ),
            "",
        )
        duplicate = _title_duplicate(
            c,
            config,
            video,
            title,
            {"source_url": canonical, "migration_id": migration_id},
            migration_id=migration_id,
        )
        prior = c.execute(
            "SELECT d.id FROM public.rag_knowledge_documents d JOIN public.rag_video_jobs j ON j.id=d.source_job_id "
            "WHERE d.library_id=%s AND (j.payload->>'source_key'=%s OR split_part(j.payload->>'source_url','?',1)=%s "
            "OR (%s::text IS NOT NULL AND j.payload->>'source_note_id'=%s))",
            (
                lid,
                source_key,
                canonical,
                source_key.split(":", 1)[1] if source_key.startswith("xhs:") else None,
                source_key.split(":", 1)[1] if source_key.startswith("xhs:") else None,
            ),
        ).fetchall()
        document_ids = list(set(document_ids) | {str(row[0]) for row in prior})
        payload = {
            "source_url": source_url if source_key.startswith("xhs") else canonical,
            "title": title,
            "migration_id": migration_id,
            "source_key": source_key,
            "supersedes": sorted(set(document_ids)),
        }
        if source_key.startswith("xhs:"):
            payload["source_note_id"] = source_key.split(":", 1)[1]
        ident = duplicate["job_id"] if duplicate else str(uuid4())
        if not duplicate:
            c.execute(
                "INSERT INTO public.rag_video_jobs(id,library_id,collection,state,payload,priority,title_key) VALUES(%s,%s,%s,'queued',%s,10,%s)",
                (ident, lid, collection, Jsonb(payload), title_text(title)),
            )
        from .title_dedup import retain_supersedes

        retain_supersedes(c, video, ident, document_ids, title_text(title))
        c.execute(
            "INSERT INTO public.rag_legacy_reprocess VALUES(%s,%s,%s,%s,%s,%s)",
            (
                lid,
                migration_id,
                source_key,
                canonical,
                ident,
                Jsonb(sorted(set(document_ids))),
            ),
        )
    return duplicate or {"job_id": ident, "state": "queued", "duplicate": False}


def enqueue_external_source(config, video, source_url, collection="all", title=""):
    from .video_sources import normalize_media_url

    key, url = normalize_media_url(source_url)
    lid = library_id(video)
    target = ingestion_collection(config, video, collection)
    with connect_database(config) as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lid,))
        duplicate = _title_duplicate(c, config, video, title, {"source_url": url})
        if duplicate:
            return duplicate
        row = c.execute(
            "SELECT id,state FROM public.rag_video_jobs WHERE library_id=%s AND (payload->>'source_key'=%s OR payload->>'source_url'=%s) ORDER BY created_at DESC LIMIT 1",
            (lid, key, url),
        ).fetchone()
        if row:
            return _duplicate_result(config, video, row, c)
        ident = str(uuid4())
        from .title_dedup import title_text

        c.execute(
            "INSERT INTO public.rag_video_jobs(id,library_id,collection,state,payload,title_key) VALUES(%s,%s,%s,'queued',%s,%s)",
            (
                ident,
                lid,
                target,
                Jsonb({"source_url": url, "source_key": key, "title": title[:300]}),
                title_text(title),
            ),
        )
    return {"job_id": ident, "state": "queued"}
