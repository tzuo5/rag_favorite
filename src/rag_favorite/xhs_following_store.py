"""Transactional following snapshots and page checkpoints in the core database."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from psycopg.types.json import Jsonb

from .database import connect_database
from .video_provider import ProviderUnavailable
from .video_store import library_id
from .xhs_private import atomic_private_json, private_directory, private_json


class FollowingStore:
    def __init__(self, config, video, collection):
        self.config, self.video, self.collection = config, video, collection
        self.library = library_id(video)

    def bind(self, owner):
        import fcntl

        from .xhs_private import private_open

        digest = hashlib.sha256(owner.encode()).hexdigest()
        root = self.video.root / "favorites"
        private_directory(root)
        binding = root / "owner.json"
        # This same lock protects first binding by both favorites and following.
        with private_open(root / "owner.lock", append=True) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous = private_json(binding) if binding.exists() else {}
            if previous.get("owner_hash") not in {None, digest}:
                raise ProviderUnavailable("XHS_ACCOUNT_CHANGED_RESTART_REQUIRED")
            with connect_database(self.config) as c:
                c.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ("xhs-owner:" + self.library,),
                )
                c.execute(
                    "INSERT INTO public.rag_xhs_accounts(library_id,owner_hash) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                    (self.library, digest),
                )
                stored = c.execute(
                    "SELECT owner_hash FROM public.rag_xhs_accounts WHERE library_id=%s",
                    (self.library,),
                ).fetchone()[0]
                if stored != digest:
                    raise ProviderUnavailable("XHS_ACCOUNT_CHANGED_RESTART_REQUIRED")
            if not previous:
                atomic_private_json(binding, {"owner_hash": digest})

    def paused(self):
        with connect_database(self.config) as c:
            row = c.execute(
                "SELECT paused FROM public.rag_xhs_accounts WHERE library_id=%s",
                (self.library,),
            ).fetchone()
        return bool(row and row[0])

    def control(self, paused):
        with connect_database(self.config) as c:
            result = c.execute(
                "UPDATE public.rag_xhs_accounts SET paused=%s,updated_at=now() WHERE library_id=%s RETURNING library_id",
                (paused, self.library),
            ).fetchone()
        return {"state": "paused" if paused else "ready", "configured": bool(result)}

    def latest(self):
        with connect_database(self.config) as c:
            row = c.execute(
                "SELECT id,state,mode,following_complete,following_count,error_code FROM public.rag_xhs_scans WHERE library_id=%s AND (collection=%s OR %s) ORDER BY created_at DESC LIMIT 1",
                (self.library, self.collection, getattr(self.config, "unified", False)),
            ).fetchone()
        return (
            dict(
                zip(
                    (
                        "scan_id",
                        "state",
                        "mode",
                        "following_complete",
                        "following_count",
                        "error_code",
                    ),
                    row,
                    strict=True,
                )
            )
            if row
            else None
        )

    def snapshot(self, snapshot, *, restart=False, refresh_heads=False):
        """Apply removals only after an exact, complete following snapshot."""
        if snapshot.get("complete") is not True or snapshot.get("count") != len(
            snapshot.get("authors", [])
        ):
            raise ProviderUnavailable("FOLLOWING_ENUMERATION_UNAVAILABLE")
        authors = snapshot["authors"]
        latest = self.latest()
        with connect_database(self.config) as c:
            history_complete_ids = {
                r[0]
                for r in c.execute(
                    "SELECT DISTINCT a.author_id FROM public.rag_xhs_author_scans a JOIN public.rag_xhs_scans s ON s.id=a.scan_id WHERE s.library_id=%s AND (s.collection=%s OR %s) AND a.checkpoint->>'terminal'='true'",
                    (
                        self.library,
                        self.collection,
                        getattr(self.config, "unified", False),
                    ),
                ).fetchall()
            }
            c.execute(
                "UPDATE public.rag_xhs_authors SET active=false WHERE library_id=%s",
                (self.library,),
            )
            for author in authors:
                c.execute(
                    "INSERT INTO public.rag_xhs_authors(library_id,author_id,nickname) VALUES(%s,%s,%s) ON CONFLICT(library_id,author_id) DO UPDATE SET active=true,nickname=EXCLUDED.nickname,last_seen=now()",
                    (self.library, author["author_id"], author["nickname"]),
                )
            if latest and latest["state"] != "complete" and not restart:
                scan_id, mode = latest["scan_id"], latest["mode"]
            else:
                full = c.execute(
                    "SELECT updated_at FROM public.rag_xhs_scans WHERE library_id=%s AND (collection=%s OR %s) AND mode IN ('history','full') AND state='complete' ORDER BY updated_at DESC LIMIT 1",
                    (
                        self.library,
                        self.collection,
                        getattr(self.config, "unified", False),
                    ),
                ).fetchone()
                mode = (
                    "history"
                    if not full
                    else (
                        "full"
                        if (datetime.now(UTC) - full[0]).total_seconds() >= 7 * 86400
                        else "incremental"
                    )
                )
                scan_id = uuid4()
                c.execute(
                    "INSERT INTO public.rag_xhs_scans(id,library_id,collection,mode,state) VALUES(%s,%s,%s,%s,'running')",
                    (scan_id, self.library, self.collection, mode),
                )
            c.execute(
                "UPDATE public.rag_xhs_scans SET following_complete=true,following_count=%s,state='running',error_code=NULL,updated_at=now() WHERE id=%s",
                (snapshot["count"], scan_id),
            )
            for author in authors:
                c.execute(
                    "INSERT INTO public.rag_xhs_author_scans(scan_id,author_id,checkpoint) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        scan_id,
                        author["author_id"],
                        Jsonb(
                            {
                                "cursor": "",
                                "seen_cursors": [],
                                "pages": 0,
                                "known_pages": 0,
                                "history": author["author_id"]
                                not in history_complete_ids,
                                "submitted": 0,
                                "duplicates": 0,
                            }
                        ),
                    ),
                )
            # Recover cancelled authors if re-followed; unblock per-author transient failures on a new invocation.
            c.execute(
                "UPDATE public.rag_xhs_author_scans SET state='pending',error_code=NULL WHERE scan_id=%s AND state IN ('blocked','cancelled') AND author_id IN (SELECT author_id FROM public.rag_xhs_authors WHERE library_id=%s AND active)",
                (scan_id, self.library),
            )
            c.execute(
                "UPDATE public.rag_xhs_author_scans SET state='cancelled' WHERE scan_id=%s AND state<>'complete' AND author_id NOT IN (SELECT author_id FROM public.rag_xhs_authors WHERE library_id=%s AND active)",
                (scan_id, self.library),
            )
            if (
                refresh_heads
                and latest
                and latest["state"] != "complete"
                and not restart
            ):
                rows = c.execute(
                    "SELECT author_id,checkpoint FROM public.rag_xhs_author_scans WHERE scan_id=%s AND state<>'cancelled'",
                    (scan_id,),
                ).fetchall()
                for author, checkpoint in rows:
                    if checkpoint.get("pages", 0):
                        checkpoint.update(
                            head_pending=True,
                            head_cursor="",
                            head_seen=[],
                            head_known_pages=0,
                        )
                        c.execute(
                            "UPDATE public.rag_xhs_author_scans SET state='pending',checkpoint=%s WHERE scan_id=%s AND author_id=%s",
                            (Jsonb(checkpoint), scan_id, author),
                        )
        return scan_id, mode

    def next_author(self, scan_id):
        with connect_database(self.config) as c:
            row = c.execute(
                "SELECT author_id,checkpoint FROM public.rag_xhs_author_scans WHERE scan_id=%s AND state='pending' ORDER BY updated_at,author_id LIMIT 1",
                (scan_id,),
            ).fetchone()
        return row

    def save_author(self, scan_id, author, checkpoint, state="pending", error=None):
        with connect_database(self.config) as c:
            c.execute(
                "UPDATE public.rag_xhs_author_scans SET checkpoint=%s,state=%s,error_code=%s,updated_at=now() WHERE scan_id=%s AND author_id=%s",
                (Jsonb(checkpoint), state, error, scan_id, author),
            )

    def known(self, note_id):
        with connect_database(self.config) as c:
            return bool(
                c.execute(
                    "SELECT 1 FROM public.rag_video_jobs WHERE library_id=%s AND (collection=%s OR %s) AND payload->>'source_note_id'=%s LIMIT 1",
                    (
                        self.library,
                        self.collection,
                        getattr(self.config, "unified", False),
                        note_id,
                    ),
                ).fetchone()
            )

    def queue_count(self):
        with connect_database(self.config) as c:
            return c.execute(
                "SELECT count(*) FROM public.rag_video_jobs WHERE library_id=%s AND state IN ('queued','running')",
                (self.library,),
            ).fetchone()[0]

    def finish(self, scan_id, state, error=None):
        with connect_database(self.config) as c:
            c.execute(
                "UPDATE public.rag_xhs_scans SET state=%s,error_code=%s,updated_at=now() WHERE id=%s",
                (state, error, scan_id),
            )
        return self.status()

    def status(self):
        latest = self.latest()
        if not latest:
            return {
                "state": "not_started",
                "discovery_complete": False,
                "processing_complete": False,
            }
        with connect_database(self.config) as c:
            authors = c.execute(
                "SELECT state,count(*) FROM public.rag_xhs_author_scans WHERE scan_id=%s GROUP BY state",
                (latest["scan_id"],),
            ).fetchall()
            totals = c.execute(
                "SELECT coalesce(sum((checkpoint->>'pages')::int),0),coalesce(sum((checkpoint->>'submitted')::int),0),coalesce(sum((checkpoint->>'duplicates')::int),0) FROM public.rag_xhs_author_scans WHERE scan_id=%s",
                (latest["scan_id"],),
            ).fetchone()
            jobs = c.execute(
                "SELECT j.state,count(*) FROM public.rag_video_jobs j WHERE j.id IN (SELECT s.job_id FROM public.rag_xhs_sources s WHERE s.library_id=%s AND s.collection=%s AND (s.origin='following' OR (s.origin='live_photo' AND s.note_id IN (SELECT note_id FROM public.rag_xhs_sources WHERE library_id=%s AND collection=%s AND origin='following')))) GROUP BY j.state",
                (self.library, self.collection, self.library, self.collection),
            ).fetchall()
            errors = c.execute(
                "SELECT author_id,error_code FROM public.rag_xhs_author_scans WHERE scan_id=%s AND error_code IS NOT NULL",
                (latest["scan_id"],),
            ).fetchall()
            gaps = c.execute(
                "SELECT id,payload->'media_gaps' FROM public.rag_video_jobs WHERE id IN (SELECT job_id FROM public.rag_xhs_sources WHERE library_id=%s AND collection=%s AND origin='following') AND jsonb_array_length(coalesce(payload->'media_gaps','[]'::jsonb))>0",
                (self.library, self.collection),
            ).fetchall()
            historical = c.execute(
                "SELECT updated_at FROM public.rag_xhs_scans WHERE library_id=%s AND collection=%s AND mode IN ('history','full') AND state='complete' ORDER BY updated_at DESC LIMIT 1",
                (self.library, self.collection),
            ).fetchone()
            discovered = c.execute(
                "SELECT count(DISTINCT note_id) FROM public.rag_xhs_sources WHERE library_id=%s AND collection=%s AND origin='following'",
                (self.library, self.collection),
            ).fetchone()[0]
        counts = dict(jobs)
        discovery_complete = (
            latest["state"] == "complete" and latest["following_complete"]
        )
        latest["scan_id"] = str(latest["scan_id"])
        from .queue_control import read_control

        refresh = self.video.root / "following" / "worker-refresh.json"
        return {
            **latest,
            "paused": self.paused(),
            "worker_paused": read_control(self.video).paused,
            "worker_refresh": private_json(refresh) if refresh.exists() else None,
            "authors": dict(authors),
            "pages": totals[0],
            "submitted": totals[1],
            "duplicates": totals[2],
            "discovered": discovered,
            "jobs": counts,
            "media_gaps": [
                {"job_id": str(job_id), "gaps": missing} for job_id, missing in gaps
            ],
            "last_full_scan_at": historical[0].isoformat() if historical else None,
            "enumeration_scope": "recent_posts"
            if latest["mode"] == "incremental"
            else "all_available_posts",
            "author_errors": [{"author_id": a, "error_code": e} for a, e in errors],
            "discovery_complete": discovery_complete,
            "processing_complete": discovery_complete
            and not gaps
            and not any(v for k, v in counts.items() if k != "complete"),
        }
