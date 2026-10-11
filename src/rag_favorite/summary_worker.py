"""Durable summary backfill; never downloads, retranscribes or reanalyses media."""

from __future__ import annotations

import argparse
import json
import signal
import time

from .config import load_config
from .database import connect_database
from .embedding import embedding_space_id
from .indexes import resolve_index
from .knowledge_summaries import summarize_job, summary_status
from .queue_control import locked_control
from .video_config import load_video_config
from .video_store import library_id


def enqueue_summaries(config, video, *, retry_failed=False) -> int:
    """Queue every completed source job, keeping failures visible until retried."""
    space = embedding_space_id(resolve_index(config).embedding)
    with connect_database(config) as c:
        result = c.execute(
            """INSERT INTO public.rag_summary_jobs(job_id,library_id,state)
            SELECT j.id,j.library_id,'queued' FROM public.rag_video_jobs j
            WHERE j.library_id=%s AND j.collection=ANY(%s) AND j.state='complete'
            AND NOT EXISTS(SELECT 1 FROM public.rag_knowledge_summaries s
                           WHERE s.job_id=j.id AND s.embedding_space_id=%s)
            ON CONFLICT(job_id) DO UPDATE SET state='queued',stage='queued',error_code=NULL,updated_at=now()
            WHERE rag_summary_jobs.state='complete'""",
            (
                library_id(video),
                (
                    [
                        "general",
                        "cooking",
                        "tech",
                        "career",
                        "finance",
                        "social-conduct",
                        "thought-politics",
                        "literature-culture",
                    ]
                    if config.unified
                    else list(config.collections)
                ),
                space,
            ),
        )
        count = result.rowcount
        if retry_failed:
            count += c.execute(
                """UPDATE public.rag_summary_jobs q SET state='queued',stage='queued',
                error_code=NULL,updated_at=now() FROM public.rag_video_jobs j
                WHERE j.id=q.job_id AND q.library_id=%s AND q.state='failed' AND j.state='complete'""",
                (library_id(video),),
            ).rowcount
    return count


def claim_summary(config, video):
    with locked_control(video) as control:
        if control.paused:
            return None
        with connect_database(config) as c:
            row = c.execute(
                """SELECT q.job_id FROM public.rag_summary_jobs q
                JOIN public.rag_video_jobs j ON j.id=q.job_id
                WHERE q.library_id=%s AND q.state='queued' AND j.state='complete'
                  AND NOT (q.job_id=ANY(%s::uuid[]))
                ORDER BY j.created_at,q.job_id LIMIT 1 FOR UPDATE OF q SKIP LOCKED""",
                (library_id(video), list(control.paused_jobs)),
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE public.rag_summary_jobs SET state='running',stage='claimed',updated_at=now() WHERE job_id=%s",
                    (row[0],),
                )
                return str(row[0])
    return None


def recover_summary_claims(config, video) -> int:
    with connect_database(config) as c:
        return c.execute(
            """UPDATE public.rag_summary_jobs q SET state='queued',stage='recovered',updated_at=now()
            FROM public.rag_video_jobs j WHERE j.id=q.job_id AND q.library_id=%s
            AND q.state='running' AND j.state='complete'
            AND pg_try_advisory_xact_lock(hashtext('summary:' || q.job_id::text))""",
            (library_id(video),),
        ).rowcount


def run_summary_worker(config, video, *, once=False, drain=False):
    with connect_database(config, register_pgvector=False) as guard:
        guard.autocommit = True
        if not guard.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s))",
            ("summary-worker:" + library_id(video),),
        ).fetchone()[0]:
            raise RuntimeError("A summary backfill worker is already running.")
        # Recover only backfill claims; inline ingestion owns its own checkpoint.
        recover_summary_claims(config, video)
        scanned = 0.0
        while True:
            if time.monotonic() - scanned >= 60:
                recover_summary_claims(config, video)
                enqueue_summaries(config, video)
                scanned = time.monotonic()
            job_id = claim_summary(config, video)
            if job_id:
                try:
                    result = summarize_job(config, video, job_id)
                    if result["state"] == "busy":
                        with connect_database(config) as c:
                            c.execute(
                                "UPDATE public.rag_summary_jobs SET state='queued',stage='lock_wait',updated_at=now() WHERE job_id=%s AND state='running'",
                                (job_id,),
                            )
                    print(json.dumps(result), flush=True)
                except Exception as exc:  # noqa: BLE001 - isolate a failed summary
                    print(
                        json.dumps(
                            {
                                "job_id": job_id,
                                "state": "failed",
                                "error_code": getattr(exc, "code", None)
                                or type(exc).__name__,
                            }
                        ),
                        flush=True,
                    )
            if once or (drain and not job_id):
                return summary_status(config, video)
            time.sleep(1 if job_id else 5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--drain",
        action="store_true",
        help="Exit after processing the current backlog.",
    )
    args = parser.parse_args()

    def stop(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        result = run_summary_worker(
            load_config(), load_video_config(), once=args.once, drain=args.drain
        )
        print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
