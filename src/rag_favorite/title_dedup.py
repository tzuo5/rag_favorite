"""Exact video-title identity, reversible queue merging and title backfill."""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

from psycopg.types.json import Jsonb

from .config import ConfigError, load_config
from .database import connect_database
from .knowledge_summaries import atomic_text
from .video_config import load_video_config
from .video_store import library_id


def title_text(value, *, legacy_filename=False):
    """Keep numbers and punctuation; remove only known filename decoration."""
    if not isinstance(value, str):
        return ""
    if legacy_filename:
        value = re.sub(r"--[a-f0-9]{8}$", "", value)
    value = " ".join(unicodedata.normalize("NFKC", value).split())
    if value.casefold() in {"", "untitled", "unknown", "无标题", "未命名", "知识记录"}:
        return ""
    if re.fullmatch(r"[a-f0-9]{24}|[a-f0-9]{32}", value):
        return ""
    if re.fullmatch(
        r"\[\d{1,2}[-:]\d{2}(?:[-:]\d{2})?\s*[-–—]\s*\d{1,2}[-:]\d{2}(?:[-:]\d{2})?\]",
        value,
    ):
        return ""
    return value


def source_key(payload):
    if payload.get("source_note_id"):
        return "xhs:" + payload["source_note_id"]
    if payload.get("source_key"):
        return payload["source_key"]
    from .video_sources import normalize_media_url

    try:
        return normalize_media_url(payload.get("source_url", ""))[0]
    except (ConfigError, ValueError):
        return ""


def canonical_job(connection, video, job_id):
    lid = library_id(video)
    seen = set()
    while str(job_id) not in seen:
        seen.add(str(job_id))
        row = connection.execute(
            "SELECT canonical_job_id FROM public.rag_title_job_aliases WHERE duplicate_job_id=%s AND library_id=%s",
            (job_id, lid),
        ).fetchone()
        if not row:
            return str(job_id)
        job_id = row[0]
    raise ConfigError("Cyclic title alias.")


def find_title_job(connection, video, title, *, migration_id=None):
    key = title_text(title)
    if not key:
        return None
    row = connection.execute(
        "SELECT id,state FROM public.rag_video_jobs WHERE library_id=%s AND title_key=%s "
        "AND state IN ('complete','running','published','queued') "
        "AND (%s::text IS NULL OR state<>'complete' OR payload->>'migration_id'=%s) "
        "ORDER BY CASE state WHEN 'complete' THEN 0 WHEN 'published' THEN 1 WHEN 'running' THEN 2 ELSE 3 END,created_at,id LIMIT 1",
        (library_id(video), key, migration_id, migration_id),
    ).fetchone()
    return row


def retain_submission(connection, video, job_id, details):
    """A skipped submission retains its source identity without inventing a job."""
    connection.execute(
        "UPDATE public.rag_video_jobs SET payload=jsonb_set(payload,'{same_title_submissions}', "
        "coalesce(payload->'same_title_submissions','[]'::jsonb) || %s),updated_at=now() "
        "WHERE id=%s AND library_id=%s AND NOT coalesce(payload->'same_title_submissions','[]'::jsonb) @> %s",
        (Jsonb([details]), job_id, library_id(video), Jsonb([details])),
    )


def retain_supersedes(connection, video, target, document_ids, key=""):
    """A refresh replaces every prior source with the same title after success."""
    row = connection.execute(
        "SELECT payload FROM public.rag_video_jobs WHERE id=%s AND library_id=%s FOR UPDATE",
        (target, library_id(video)),
    ).fetchone()
    if not row:
        raise ConfigError("Missing canonical title job.")
    ids = set(row[0].get("supersedes", [])) | set(document_ids)
    if key:
        ids.update(
            str(r[0])
            for r in connection.execute(
                "SELECT id FROM public.rag_knowledge_documents WHERE library_id=%s AND title_key=%s",
                (library_id(video), key),
            ).fetchall()
        )
    ids.discard(str(target))
    connection.execute(
        "UPDATE public.rag_video_jobs SET payload=payload || %s WHERE id=%s AND library_id=%s",
        (Jsonb({"supersedes": sorted(ids)}), target, library_id(video)),
    )


def _merge(connection, video, duplicate, target, key):
    ident, state, stage, payload = duplicate[:4]
    if str(ident) == str(target) or state in {"complete", "published", "duplicate"}:
        return False
    lid = library_id(video)
    retain_submission(
        connection,
        video,
        target,
        {
            "job_id": str(ident),
            "source_url": payload.get("source_url"),
            "source_note_id": payload.get("source_note_id"),
        },
    )
    retain_supersedes(connection, video, target, payload.get("supersedes", []), key)
    connection.execute(
        "INSERT INTO public.rag_title_job_aliases(duplicate_job_id,library_id,canonical_job_id,title_key,previous_state,previous_stage,previous_payload) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(duplicate_job_id) DO UPDATE SET canonical_job_id=excluded.canonical_job_id",
        (ident, lid, target, key, state, stage, Jsonb(payload)),
    )
    connection.execute(
        "UPDATE public.rag_video_jobs SET state='duplicate',stage='title_duplicate',error_code=NULL, "
        "payload=payload || %s,updated_at=now() WHERE id=%s AND library_id=%s",
        (Jsonb({"duplicate_of": str(target)}), ident, lid),
    )
    return True


def deduplicate_job(config, video, job):
    """Called before costly media/ASR work, including after source-title discovery."""
    if not getattr(config, "unified", False):
        return False
    key = title_text(job["payload"].get("title"))
    if not key:
        return False
    lid, ident = library_id(video), str(job["id"])
    with connect_database(config) as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lid,))
        c.execute(
            "UPDATE public.rag_video_jobs SET title_key=%s,payload=payload || %s WHERE id=%s AND library_id=%s",
            (key, Jsonb({"title": job["payload"]["title"]}), ident, lid),
        )
        target = find_title_job(
            c, video, key, migration_id=job["payload"].get("migration_id")
        )
        if not target:
            return False
        if job["payload"].get("migration_id") and target[1] != "complete":
            retain_supersedes(
                c, video, target[0], job["payload"].get("supersedes", []), key
            )
        rows = c.execute(
            "SELECT id,state,stage,payload FROM public.rag_video_jobs WHERE library_id=%s AND title_key=%s "
            "AND (state IN ('queued','blocked','failed') OR id=%s) FOR UPDATE",
            (lid, key, ident),
        ).fetchall()
        for row in rows:
            _merge(c, video, row, target[0], key)
        return str(target[0]) != ident


def backfill(config, video, run, *, apply=False):
    """Retain every original; fold duplicate tasks and title identities only."""
    if not config.unified:
        raise ConfigError("Title deduplication requires the unified library.")
    lid = library_id(video)
    with connect_database(config) as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lid,))
        jobs = c.execute(
            "SELECT id,state,stage,payload,created_at,title_key FROM public.rag_video_jobs WHERE library_id=%s ORDER BY created_at,id FOR UPDATE",
            (lid,),
        ).fetchall()
        docs = c.execute(
            "SELECT id,source_kind,title,metadata,source_job_id,title_key FROM public.rag_knowledge_documents WHERE library_id=%s ORDER BY id",
            (lid,),
        ).fetchall()
        sources = {}
        for ident, state, stage, payload, created, old_key in jobs:
            title = title_text(payload.get("title"))
            file = video.root / "derived" / str(ident) / "source.json"
            if file.is_file() and not file.is_symlink():
                title = title_text(json.loads(file.read_text()).get("title")) or title
            if title and source_key(payload):
                sources[source_key(payload)] = title
        document_titles = {}
        job_titles = {}
        for ident, kind, title, meta, job_id, old_key in docs:
            if not (job_id or meta.get("source_urls") or kind == "media"):
                continue
            value = title_text(title, legacy_filename=not job_id)
            for url in meta.get("source_urls", []):
                value = sources.get(source_key({"source_url": url}), value)
            if job_id:
                payload = next((j[3] for j in jobs if j[0] == job_id), {})
                value = (
                    sources.get(source_key(payload), title_text(payload.get("title")))
                    or value
                )
            document_titles[str(ident)] = value
        groups = defaultdict(list)
        for row in jobs:
            ident, state, _stage, payload, _created, _old_key = row
            value = sources.get(source_key(payload), title_text(payload.get("title")))
            if not value:
                value = next(
                    (
                        document_titles[i]
                        for i in payload.get("supersedes", [])
                        if document_titles.get(i)
                    ),
                    "",
                )
            job_titles[str(ident)] = value
            if value and state != "duplicate":
                groups[value].append(row)
        document_groups = defaultdict(list)
        for ident, value in document_titles.items():
            if value:
                document_groups[value].append(ident)
        merges = []
        refresh_targets = []
        for key, rows in groups.items():
            live = [r for r in rows if r[1] in {"running", "published"}]
            refresh = [r for r in rows if r[1] == "queued" and r[3].get("migration_id")]
            complete = [r for r in rows if r[1] == "complete"]
            pool = live or refresh or complete or rows
            target = min(
                pool,
                key=lambda r: (
                    {
                        "running": 0,
                        "published": 1,
                        "queued": 2,
                        "complete": 3,
                        "blocked": 4,
                        "failed": 5,
                    }.get(r[1], 6),
                    r[4],
                    str(r[0]),
                ),
            )
            if target[1] not in {"complete", "duplicate"} and any(
                r[3].get("migration_id") for r in rows
            ):
                refresh_targets.append((target[0], key))
            for row in rows:
                if row[0] != target[0] and row[1] in {"queued", "blocked", "failed"}:
                    merges.append((row, target[0], key))
        report = {
            "applied": apply,
            "duplicate_document_title_groups": sum(
                len(v) > 1 for v in document_groups.values()
            ),
            "duplicate_document_title_excess": sum(
                max(0, len(v) - 1) for v in document_groups.values()
            ),
            "queued_tasks_merged": sum(
                row[1] == "queued" for row, target, key in merges
            ),
            "other_tasks_merged": sum(
                row[1] != "queued" for row, target, key in merges
            ),
            "unknown_video_titles": sum(not value for value in job_titles.values()),
            "merges": [
                {
                    "duplicate_job_id": str(row[0]),
                    "canonical_job_id": str(target),
                    "title": key,
                    "previous_state": row[1],
                }
                for row, target, key in merges
            ],
        }
        if apply:
            run.mkdir(parents=True, exist_ok=True, mode=0o700)
            before = run / "before.json"
            if not before.exists():
                atomic_text(
                    before,
                    json.dumps(
                        {"library_id": lid, "jobs": jobs, "documents": docs},
                        ensure_ascii=False,
                        default=str,
                        indent=2,
                    ),
                )
            for ident, value in job_titles.items():
                c.execute(
                    "UPDATE public.rag_video_jobs SET title_key=%s,payload=payload || %s WHERE id=%s AND library_id=%s",
                    (
                        value,
                        Jsonb({"title": value}) if value else Jsonb({}),
                        ident,
                        lid,
                    ),
                )
            for ident, value in document_titles.items():
                c.execute(
                    "UPDATE public.rag_knowledge_documents SET title_key=%s,title=CASE WHEN %s<>'' THEN %s ELSE title END WHERE id=%s AND library_id=%s",
                    (value, value, value, ident, lid),
                )
            for target, key in refresh_targets:
                retain_supersedes(c, video, target, [], key)
            for row, target, key in merges:
                _merge(c, video, row, target, key)
            atomic_text(
                run / "report.json", json.dumps(report, ensure_ascii=False, indent=2)
            )
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--run", type=Path, default=Path(".runtime/migrations/title-dedup-20261010")
    )
    args = parser.parse_args()
    report = backfill(load_config(), load_video_config(), args.run, apply=args.apply)
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "merges"}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
