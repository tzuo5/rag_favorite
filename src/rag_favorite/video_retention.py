"""Expire failed task copies under the worker lock; preserve recovered knowledge."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from uuid import UUID

from psycopg.types.json import Jsonb

from .config import ConfigError
from .database import connect_database
from .video_store import ASSET_ID, library_id


def expire_failed_media(config, video, *, apply=False, worker_owns_lock=False):
    with connect_database(config, register_pgvector=False) as guard:
        guard.autocommit = True
        if (
            apply
            and not worker_owns_lock
            and not guard.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s))",
                ("video-worker:" + library_id(video),),
            ).fetchone()[0]
        ):
            raise ConfigError("Stop the video worker before applying manual expiry.")
        with connect_database(config) as c:
            rows = c.execute(
                "SELECT id,state,payload FROM public.rag_video_jobs WHERE library_id=%s AND state IN ('failed','blocked') AND stage<>'media_expired' AND updated_at < now()-(%s * interval '1 hour') ORDER BY updated_at FOR UPDATE SKIP LOCKED",
                (library_id(video), video.failed_media_ttl_hours),
            ).fetchall()
            results = []
            for job_id, state, payload in rows:
                job_id = str(UUID(str(job_id)))
                report = {"job_id": job_id, "state": state, "expired": False}
                if apply:
                    from .video_worker import atomic_json

                    assets = video.root / "assets"
                    work_root = video.root / "work"
                    derived_root = video.root / "derived"
                    work, derived = work_root / job_id, derived_root / job_id
                    if any(
                        path.is_symlink()
                        for path in (assets, work_root, derived_root, work, derived)
                    ):
                        raise ConfigError("Unsafe task path; media expiry refused.")
                    ids = list(
                        dict.fromkeys(
                            value
                            for value in [
                                payload.get("asset_id"),
                                payload.get("transcript_asset_id"),
                                *payload.get("superseded_asset_ids", []),
                            ]
                            if value
                        )
                    )
                    if any(not ASSET_ID.fullmatch(value) for value in ids):
                        raise ConfigError("Unsafe asset ID; media expiry refused.")
                    if any((assets / value).is_symlink() for value in ids):
                        raise ConfigError("Unsafe asset path; media expiry refused.")
                    retained = []
                    for asset_id in ids:
                        shared = c.execute(
                            "SELECT 1 FROM public.rag_video_jobs WHERE library_id=%s AND id<>%s AND stage<>'media_expired' AND state<>'complete' AND (payload->>'asset_id'=%s OR payload->>'transcript_asset_id'=%s OR payload->'superseded_asset_ids' ? %s) LIMIT 1",
                            (library_id(video), job_id, asset_id, asset_id, asset_id),
                        ).fetchone()
                        if shared:
                            retained.append(asset_id)
                    # Preserve timed text before deleting audio/clips/temporary frames.
                    transcript = work / "transcript.json"
                    if transcript.is_file() and not transcript.is_symlink():
                        atomic_json(
                            derived / "recovered-transcript.json",
                            json.loads(transcript.read_text()),
                        )
                    elif (
                        payload.get("transcript_asset_id")
                        and not (derived / "recovered-transcript.json").exists()
                        and (assets / payload["transcript_asset_id"]).is_file()
                    ):
                        from .video_store import asset_path
                        from .video_transcript import parse_transcript

                        subtitle, metadata = asset_path(
                            video, payload["transcript_asset_id"]
                        )
                        atomic_json(
                            derived / "recovered-transcript.json",
                            parse_transcript(subtitle, metadata["suffix"]),
                        )
                    chunks = [
                        json.loads(p.read_text())
                        for p in sorted(work.glob("asr-*.json"))
                        if not p.is_symlink()
                    ]
                    if chunks:
                        atomic_json(
                            derived / "recovered-asr-chunks.json",
                            {"partial": True, "chunks": chunks},
                        )
                    if work.exists():
                        shutil.rmtree(work)
                    for asset_id in ids:
                        if asset_id not in retained:
                            (assets / asset_id).unlink(missing_ok=True)
                            (assets / (asset_id + ".json")).unlink(missing_ok=True)
                    report.update(expired=True, shared_assets_retained=len(retained))
                    atomic_json(derived / "media-expiry.json", report)
                    payload["media_expired"] = True
                    c.execute(
                        "UPDATE public.rag_video_jobs SET stage='media_expired',payload=%s,updated_at=now() WHERE id=%s AND library_id=%s",
                        (Jsonb(payload), job_id, library_id(video)),
                    )
                results.append(report)
    return {
        "applied": apply,
        "ttl_hours": video.failed_media_ttl_hours,
        "jobs": results,
    }


def retry_job(config, video, job_id, *, visual_seconds=None, redownload=False):
    if (
        visual_seconds is not None
        and visual_seconds != 0
        and not 60 <= visual_seconds <= 3600
    ):
        raise ConfigError(
            "Visual budget must be zero (unlimited), or between 60 and 3600 seconds."
        )
    job_id = str(UUID(job_id))
    with connect_database(config) as c:
        row = c.execute(
            "SELECT state,payload,stage FROM public.rag_video_jobs WHERE id=%s AND library_id=%s FOR UPDATE",
            (job_id, library_id(video)),
        ).fetchone()
        if not row or row[0] not in {"blocked", "failed"}:
            raise ConfigError("Only blocked or failed jobs can be retried.")
        derived = video.root / "derived" / job_id
        saved_draft = all(
            path.is_file() and not path.is_symlink()
            for path in (derived / "knowledge.md", derived / "record.json")
        )
        payload = dict(row[1])
        expired = payload.get("media_expired") or row[2] == "media_expired"
        if expired and not saved_draft:
            redownload = True
        if redownload:
            from .video_sources import normalize_media_url

            if not payload.get("source_url"):
                raise ConfigError(
                    "Task media expired; submit a new source instead of retrying."
                )
            normalize_media_url(payload["source_url"])
            # Keep original provenance and derived evidence. Changed source hashes
            # invalidate extraction checkpoints when the source is fetched again.
            payload["recovery_history"] = [
                *payload.get("recovery_history", []),
                {
                    "at": datetime.now(UTC).isoformat(),
                    "reason": row[2],
                    "asset_id": payload.get("asset_id"),
                    "sha256": payload.get("sha256"),
                },
            ][-20:]
            payload["superseded_asset_ids"] = list(
                dict.fromkeys(
                    [
                        *payload.get("superseded_asset_ids", []),
                        *[
                            payload[key]
                            for key in ("asset_id", "transcript_asset_id")
                            if payload.get(key)
                        ],
                    ]
                )
            )
            image_source = derived / "image-source.json"
            if derived.is_symlink() or image_source.is_symlink():
                raise ConfigError("Unsafe recovered source path.")
            if image_source.is_file():
                previous = derived / (
                    "image-source-before-recovery-"
                    + str(len(payload["recovery_history"]))
                    + ".json"
                )
                if previous.is_symlink():
                    raise ConfigError("Unsafe recovered source history.")
                image_source.replace(previous)
            for key in ("asset_id", "transcript_asset_id", "sha256", "media_expired"):
                payload.pop(key, None)
            if c.execute("SELECT to_regclass('public.rag_video_pipeline')").fetchone()[
                0
            ]:
                c.execute(
                    "DELETE FROM public.rag_video_pipeline WHERE job_id=%s AND library_id=%s",
                    (job_id, library_id(video)),
                )
        if visual_seconds is not None:
            payload["visual_budget_seconds"] = visual_seconds
        c.execute(
            "UPDATE public.rag_video_jobs SET payload=%s,state='queued',stage='queued',error_code=NULL,updated_at=now() WHERE id=%s AND library_id=%s",
            (Jsonb(payload), job_id, library_id(video)),
        )


def attach_transcript(config, video, job_id, transcript_asset_id):
    from .video_store import asset_path

    job_id = str(UUID(job_id))
    _, subtitle = asset_path(video, transcript_asset_id)
    if subtitle["suffix"] not in {".srt", ".vtt", ".txt", ".json"}:
        raise ConfigError("Expected a transcript asset.")
    with connect_database(config) as c:
        row = c.execute(
            "SELECT state,payload,collection FROM public.rag_video_jobs WHERE id=%s AND library_id=%s FOR UPDATE",
            (job_id, library_id(video)),
        ).fetchone()
        if not row or row[0] not in {"blocked", "failed"}:
            raise ConfigError("Attach transcripts only to blocked or failed jobs.")
        if row[1].get("media_expired") or not row[1].get("asset_id"):
            raise ConfigError("Original task copy is unavailable; submit a new source.")
        if config.collection(row[2]).read_only:
            raise ConfigError("Selected collection is read-only.")
        asset_path(video, row[1]["asset_id"])
        payload = dict(row[1])
        old = payload.get("transcript_asset_id")
        if old and old != transcript_asset_id:
            payload["superseded_asset_ids"] = [
                *payload.get("superseded_asset_ids", []),
                old,
            ]
        payload["transcript_asset_id"] = transcript_asset_id
        c.execute(
            "UPDATE public.rag_video_jobs SET payload=%s,state='queued',stage='queued',error_code=NULL,updated_at=now() WHERE id=%s AND library_id=%s",
            (Jsonb(payload), job_id, library_id(video)),
        )
