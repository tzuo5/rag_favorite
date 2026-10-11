"""Four bounded, durable stage consumers sharing one library coordinator."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

from psycopg.errors import LockNotAvailable

from .config import ConfigError
from .database import connect_database
from .model_activity import job_activity
from .pipeline_fence import (
    OwnershipLost,
    PipelinePaused,
    TemporaryBudgetExceeded,
    stage_owner,
)
from .queue_control import locked_control, read_control, set_control
from .video_store import job_status, library_id

STAGES = ("download", "prepare", "llm", "publish")
LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 15


def stage_lock(job_id, stage):
    return f"video-stage:{job_id}:{stage}"


def temporary_bytes(config, video):
    """Count pipeline-owned task copies, including retained failed attempts."""
    with connect_database(config) as c:
        rows = c.execute(
            "SELECT DISTINCT j.id,j.payload FROM public.rag_video_jobs j "
            "JOIN public.rag_video_pipeline p ON p.job_id=j.id "
            "WHERE p.library_id=%s AND j.state NOT IN ('complete','duplicate')",
            (library_id(video),),
        ).fetchall()
    paths = set()
    for job_id, payload in rows:
        work = video.root / "work" / str(job_id)
        if work.is_symlink():
            raise ConfigError("Unsafe pipeline work directory")
        if work.exists():
            paths.update(
                p for p in work.rglob("*") if p.is_file() and not p.is_symlink()
            )
        for key in ("asset_id", "transcript_asset_id"):
            if payload.get(key):
                asset = video.root / "assets" / payload[key]
                if asset.is_symlink():
                    raise ConfigError("Unsafe pipeline asset")
                paths.add(asset)
    total = 0
    for path in paths:
        try:
            if path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            # Publication may safely remove an owned copy during this snapshot.
            continue
    return total


def initialize_jobs(config, video, allowed_ids=None):
    """Admit a bounded frontier; old running jobs resume through existing checkpoints."""
    with locked_control(video) as control:
        if control.paused:
            return 0
        with connect_database(config) as c:
            regenerate = c.execute(
                "DELETE FROM public.rag_video_pipeline p USING public.rag_video_jobs j "
                "WHERE j.id=p.job_id AND p.library_id=%s AND j.state='queued' AND p.status='blocked' "
                "AND p.stage='publish' AND p.error_code IN ('SUMMARY_GENERATION_UNAVAILABLE','SUMMARY_GENERATION_MISMATCH') "
                "RETURNING p.job_id",
                (library_id(video),),
            ).fetchall()
            for (job_id,) in regenerate:
                c.execute(
                    "INSERT INTO public.rag_video_pipeline(job_id,library_id,stage) VALUES(%s,%s,'llm') "
                    "ON CONFLICT(job_id,stage) DO UPDATE SET status='queued',token=rag_video_pipeline.token+1,"
                    "owner=NULL,lease_until=NULL,attempts=0,retry_at=now(),error_code=NULL,updated_at=now()",
                    (job_id, library_id(video)),
                )
            repair = c.execute(
                "DELETE FROM public.rag_video_pipeline p USING public.rag_video_jobs j "
                "WHERE j.id=p.job_id AND p.library_id=%s AND j.state='queued' AND p.status='blocked' "
                "AND p.stage='llm' AND (p.error_code LIKE 'PIPELINE_PREPAR%%' OR p.error_code='XHS_IMAGE_NOTE_INCOMPLETE') "
                "RETURNING p.job_id",
                (library_id(video),),
            ).fetchall()
            for (job_id,) in repair:
                c.execute(
                    "INSERT INTO public.rag_video_pipeline(job_id,library_id,stage) VALUES(%s,%s,'prepare') "
                    "ON CONFLICT(job_id,stage) DO UPDATE SET status='queued',token=rag_video_pipeline.token+1,"
                    "owner=NULL,lease_until=NULL,attempts=0,retry_at=now(),error_code=NULL,updated_at=now()",
                    (job_id, library_id(video)),
                )
            # Explicit manual retry reopens only its failed stage, not successful work.
            c.execute(
                "UPDATE public.rag_video_pipeline p SET status='queued',attempts=0,"
                "error_code=NULL,retry_at=now(),updated_at=now() FROM public.rag_video_jobs j "
                "WHERE j.id=p.job_id AND p.library_id=%s AND p.status='blocked' AND j.state='queued'",
                (library_id(video),),
            )
            count = c.execute(
                "SELECT count(DISTINCT job_id) FROM public.rag_video_pipeline "
                "WHERE library_id=%s AND status IN ('queued','running')",
                (library_id(video),),
            ).fetchone()[0]
            room = max(0, video.pipeline_prefetch + 3 - count)
            if not room or temporary_bytes(config, video) >= video.pipeline_temp_bytes:
                return 0
            rows = c.execute(
                "SELECT j.id FROM public.rag_video_jobs j WHERE j.library_id=%s "
                "AND j.state IN ('queued','running','published') "
                "AND NOT (j.id=ANY(%s::uuid[])) "
                "AND (%s::uuid[] IS NULL OR j.id=ANY(%s::uuid[])) "
                "AND NOT EXISTS (SELECT 1 FROM public.rag_video_pipeline p WHERE p.job_id=j.id) "
                "ORDER BY j.priority DESC,j.created_at LIMIT %s FOR UPDATE SKIP LOCKED",
                (
                    library_id(video),
                    list(control.paused_jobs),
                    allowed_ids,
                    allowed_ids,
                    room,
                ),
            ).fetchall()
            for (job_id,) in rows:
                c.execute(
                    "INSERT INTO public.rag_video_pipeline(job_id,library_id,stage) "
                    "VALUES(%s,%s,'download') ON CONFLICT DO NOTHING",
                    (job_id, library_id(video)),
                )
            return len(rows)


def recover_leases(config, video):
    with connect_database(config) as c:
        # Completion/duplicate publication may commit just before a process dies.
        c.execute(
            "UPDATE public.rag_video_pipeline p SET status='done',owner=NULL,lease_until=NULL,"
            "completed_at=now(),updated_at=now() FROM public.rag_video_jobs j "
            "WHERE j.id=p.job_id AND p.library_id=%s AND p.status<>'done' "
            "AND j.state IN ('complete','duplicate') "
            "AND pg_try_advisory_xact_lock(hashtext('video-stage:' || p.job_id::text || ':' || p.stage))",
            (library_id(video),),
        )
        return c.execute(
            "UPDATE public.rag_video_pipeline SET status='queued',owner=NULL,lease_until=NULL,"
            "token=token+1,error_code='LEASE_RECOVERED',updated_at=now() "
            "WHERE library_id=%s AND status='running' AND lease_until<now() "
            "AND pg_try_advisory_xact_lock(hashtext('video-stage:' || job_id::text || ':' || stage))",
            (library_id(video),),
        ).rowcount


def claim_stage(config, video, stage, owner, guard, allowed_ids=None):
    with locked_control(video) as control:
        if control.paused:
            return None
        with connect_database(config) as c:
            if stage == "llm":
                circuit = c.execute(
                    "SELECT circuit_until>now() FROM public.rag_video_pipeline_control WHERE library_id=%s",
                    (library_id(video),),
                ).fetchone()
                if circuit and circuit[0]:
                    return None
            if stage == "prepare":
                prepared = c.execute(
                    "SELECT count(*) FROM public.rag_video_pipeline WHERE library_id=%s "
                    "AND stage='llm' AND status='queued'",
                    (library_id(video),),
                ).fetchone()[0]
                if prepared >= video.pipeline_prefetch:
                    return None
            row = c.execute(
                "SELECT p.job_id,j.collection,j.state,j.payload,p.token,p.attempts "
                "FROM public.rag_video_pipeline p JOIN public.rag_video_jobs j ON j.id=p.job_id "
                "WHERE p.library_id=%s AND p.stage=%s AND p.status='queued' AND p.retry_at<=now() "
                "AND j.state NOT IN ('failed','blocked','complete','duplicate') "
                "AND NOT (p.job_id=ANY(%s::uuid[])) "
                "AND (%s::uuid[] IS NULL OR p.job_id=ANY(%s::uuid[])) "
                "ORDER BY j.priority DESC,j.created_at LIMIT 1 FOR UPDATE OF p SKIP LOCKED",
                (
                    library_id(video),
                    stage,
                    list(control.paused_jobs),
                    allowed_ids,
                    allowed_ids,
                ),
            ).fetchone()
            if not row:
                return None
            key = stage_lock(str(row[0]), stage)
            if not guard.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s))", (key,)
            ).fetchone()[0]:
                return None
            c.execute(
                "UPDATE public.rag_video_pipeline SET status='running',owner=%s,token=token+1,"
                "attempts=attempts+1,lease_until=now()+(%s*interval '1 second'),"
                "started_at=now(),error_code=NULL,updated_at=now() WHERE job_id=%s AND stage=%s",
                (owner, LEASE_SECONDS, row[0], stage),
            )
            c.execute(
                "UPDATE public.rag_video_jobs SET state=CASE WHEN state='published' THEN state ELSE 'running' END,"
                "stage=%s,error_code=NULL,attempts=attempts+%s,updated_at=now() WHERE id=%s",
                (stage, 1 if stage == "download" else 0, row[0]),
            )
            return {
                "id": str(row[0]),
                "collection": row[1],
                "state": row[2],
                "payload": row[3],
                "token": row[4] + 1,
                "attempts": row[5] + 1,
            }


class Lease:
    def __init__(self, config, video, job, stage, owner, stopping):
        self.config, self.video, self.job, self.stage, self.owner = (
            config,
            video,
            job,
            stage,
            owner,
        )
        self.stopping = stopping
        self.lost = threading.Event()
        self.finished = threading.Event()

    def assert_live(self, connection=None):
        if self.lost.is_set():
            raise OwnershipLost()
        if connection is None:
            with connect_database(self.config) as c:
                self.assert_live(c)
            return
        row = connection.execute(
            "SELECT 1 FROM public.rag_video_pipeline WHERE job_id=%s AND stage=%s "
            "AND status='running' AND owner=%s AND token=%s AND lease_until>now() FOR SHARE",
            (self.job["id"], self.stage, self.owner, self.job["token"]),
        ).fetchone()
        if not row:
            self.lost.set()
            raise OwnershipLost()

    def before_operation(self):
        self.assert_live()
        control = read_control(self.video)
        if (
            self.stopping.is_set()
            or control.paused
            or self.job["id"] in control.paused_jobs
        ):
            raise PipelinePaused()
        if (
            self.stage in {"download", "prepare"}
            and temporary_bytes(self.config, self.video)
            >= self.video.pipeline_temp_bytes
        ):
            raise TemporaryBudgetExceeded()

    def heartbeat(self):
        while not self.finished.wait(HEARTBEAT_SECONDS):
            try:
                with connect_database(self.config) as c:
                    changed = c.execute(
                        "UPDATE public.rag_video_pipeline SET lease_until=now()+(%s*interval '1 second'),updated_at=now() "
                        "WHERE job_id=%s AND stage=%s AND owner=%s AND token=%s AND status='running' AND lease_until>now()",
                        (
                            LEASE_SECONDS,
                            self.job["id"],
                            self.stage,
                            self.owner,
                            self.job["token"],
                        ),
                    ).rowcount
                if not changed:
                    self.lost.set()
                    return
            except LockNotAvailable:
                # Publication holds a shared fence until its transaction commits.
                # A busy row does not establish a lost lease; its owner still holds
                # the session lock and the next fence checks the actual deadline.
                continue
            except Exception:  # noqa: BLE001 - fail closed on loss of database heartbeat
                self.lost.set()
                return

    def request_succeeded(self):
        if self.stage != "llm":
            return
        with connect_database(self.config) as c:
            self.assert_live(c)
            c.execute(
                "INSERT INTO public.rag_video_pipeline_control(library_id) VALUES(%s) "
                "ON CONFLICT(library_id) DO UPDATE SET consecutive_failures=0,circuit_until=NULL,updated_at=now()",
                (library_id(self.video),),
            )


def finish_stage(config, video, job, stage, owner, elapsed):
    with connect_database(config) as c:
        updated = c.execute(
            "UPDATE public.rag_video_pipeline SET status='done',owner=NULL,lease_until=NULL,"
            "completed_at=now(),elapsed_seconds=elapsed_seconds+%s,updated_at=now(),artifact_ref=%s "
            "WHERE job_id=%s AND stage=%s AND owner=%s AND token=%s AND status='running' AND lease_until>now()",
            (elapsed, f"derived/{job['id']}", job["id"], stage, owner, job["token"]),
        ).rowcount
        if not updated:
            raise OwnershipLost()
        state = c.execute(
            "SELECT state FROM public.rag_video_jobs WHERE id=%s", (job["id"],)
        ).fetchone()[0]
        if state not in {"complete", "duplicate"} and stage != "publish":
            following = STAGES[STAGES.index(stage) + 1]
            c.execute(
                "INSERT INTO public.rag_video_pipeline(job_id,library_id,stage) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                (job["id"], library_id(video), following),
            )
            c.execute(
                "UPDATE public.rag_video_jobs SET stage=%s,updated_at=now() WHERE id=%s",
                (following, job["id"]),
            )
        if stage == "llm":
            c.execute(
                "INSERT INTO public.rag_video_pipeline_control(library_id) VALUES(%s) "
                "ON CONFLICT(library_id) DO UPDATE SET consecutive_failures=0,circuit_until=NULL,updated_at=now()",
                (library_id(video),),
            )


def fail_stage(config, video, job, stage, owner, exc, elapsed):
    symbolic = (
        str(exc)
        if isinstance(exc, ConfigError)
        and re.fullmatch(r"[A-Z][A-Z0-9_]{1,80}", str(exc))
        else None
    )
    code = getattr(exc, "code", None) or symbolic or type(exc).__name__
    paused = isinstance(exc, PipelinePaused)
    transient = bool(getattr(exc, "retryable", False)) or code in {
        "XHS_SESSION_BUSY",
        "SUMMARY_BUSY",
    }
    retry = paused or (transient and job["attempts"] <= 2)
    delay = 0 if paused else (5 if job["attempts"] == 1 else 30)
    with connect_database(config) as c:
        changed = c.execute(
            "UPDATE public.rag_video_pipeline SET status=%s,owner=NULL,lease_until=NULL,error_code=%s,"
            "attempts=attempts-%s,retry_at=now()+(%s*interval '1 second'),"
            "elapsed_seconds=elapsed_seconds+%s,updated_at=now() "
            "WHERE job_id=%s AND stage=%s AND owner=%s AND token=%s AND status='running' AND lease_until>now()",
            (
                "queued" if retry else "blocked",
                code,
                1 if paused else 0,
                delay,
                elapsed,
                job["id"],
                stage,
                owner,
                job["token"],
            ),
        ).rowcount
        if not changed:
            raise OwnershipLost()
        c.execute(
            "UPDATE public.rag_video_jobs SET state=CASE WHEN state='published' AND %s THEN state ELSE %s END,"
            "stage=%s,error_code=%s,updated_at=now() WHERE id=%s",
            (
                retry,
                "running" if retry else "blocked",
                f"{stage}:retry_wait" if retry else f"failed:{stage}",
                code,
                job["id"],
            ),
        )
        link_failure = bool(getattr(exc, "retryable", False)) or getattr(
            exc, "status_code", None
        ) in {401, 403, 507}
        if stage == "llm" and link_failure and not paused:
            c.execute(
                "INSERT INTO public.rag_video_pipeline_control(library_id,consecutive_failures) VALUES(%s,1) "
                "ON CONFLICT(library_id) DO UPDATE SET consecutive_failures=rag_video_pipeline_control.consecutive_failures+1,"
                "circuit_until=CASE WHEN rag_video_pipeline_control.consecutive_failures+1>=3 THEN now()+interval '60 seconds' "
                "ELSE rag_video_pipeline_control.circuit_until END,updated_at=now()",
                (library_id(video),),
            )
    print(
        json.dumps(
            {
                "job_id": job["id"],
                "pipeline_stage": stage,
                "error_code": code,
                "retry": retry,
            }
        ),
        flush=True,
    )


def pipeline_snapshot(config, video):
    with connect_database(config) as c:
        if not c.execute("SELECT to_regclass('public.rag_video_pipeline')").fetchone()[
            0
        ]:
            return {"available": False, "stages": {}, "running": [], "jobs": []}
        rows = c.execute(
            "SELECT job_id,stage,status,owner,retry_at,error_code,elapsed_seconds FROM public.rag_video_pipeline "
            "WHERE library_id=%s ORDER BY updated_at DESC",
            (library_id(video),),
        ).fetchall()
        circuit = c.execute(
            "SELECT circuit_until FROM public.rag_video_pipeline_control WHERE library_id=%s",
            (library_id(video),),
        ).fetchone()
    now = datetime.now(UTC)
    stages = {
        stage: {"waiting": 0, "running": 0, "retry_wait": 0, "blocked": 0}
        for stage in STAGES
    }
    running, jobs = [], []
    for job_id, stage, status, owner, retry_at, error, elapsed in rows:
        if status != "done":
            key = (
                "retry_wait"
                if status == "queued" and retry_at > now
                else "waiting"
                if status == "queued"
                else status
            )
            stages[stage][key] += 1
            item = {
                "job_id": str(job_id),
                "stage": stage,
                "status": key,
                "owner": owner,
                "retry_at": retry_at.isoformat(),
                "error_code": error,
                "elapsed_seconds": elapsed,
            }
            jobs.append(item)
            if status == "running":
                running.append(item)
    return {
        "available": True,
        "stages": stages,
        "running": running,
        "jobs": jobs,
        "circuit_until": circuit[0].isoformat() if circuit and circuit[0] else None,
    }


def run_pipeline(config, video, *, max_jobs=None):
    """Drain a fixed acceptance batch or continuously admit a bounded backlog."""
    from .video_worker import atomic_json, process

    if max_jobs is not None and max_jobs < 1:
        raise ConfigError("max_jobs must be positive")
    if max_jobs is not None and read_control(video).paused:
        return {"paused": True, "jobs": []}
    stopping = threading.Event()
    with connect_database(config, register_pgvector=False) as coordinator:
        coordinator.autocommit = True
        if not coordinator.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s))",
            ("video-worker:" + library_id(video),),
        ).fetchone()[0]:
            raise RuntimeError("A video worker is already running for this library.")
        with connect_database(config) as c:
            if not c.execute(
                "SELECT to_regclass('public.rag_video_pipeline')"
            ).fetchone()[0]:
                raise ConfigError(
                    "Apply database migrations before running the stage pipeline."
                )
        atomic_json(
            video.root / "queue-worker.json",
            {
                "pid": os.getpid(),
                "queue_control_version": 1,
                "title_dedup_version": 1,
                "pipeline_version": 1,
            },
        )
        allowed_ids = None
        if max_jobs is not None:
            with connect_database(config) as c:
                allowed_ids = [
                    str(r[0])
                    for r in c.execute(
                        "SELECT id FROM public.rag_video_jobs j WHERE library_id=%s AND state IN ('queued','running','published') "
                        "AND NOT (id=ANY(%s::uuid[])) ORDER BY COALESCE((SELECT max(CASE stage WHEN 'publish' THEN 4 "
                        "WHEN 'llm' THEN 3 WHEN 'prepare' THEN 2 ELSE 1 END) FROM public.rag_video_pipeline p "
                        "WHERE p.job_id=j.id AND p.status IN ('queued','running')),0) DESC,"
                        "CASE WHEN state IN ('running','published') THEN 0 ELSE 1 END,priority DESC,created_at LIMIT %s",
                        (
                            library_id(video),
                            list(read_control(video).paused_jobs),
                            max_jobs,
                        ),
                    ).fetchall()
                ]
        admitted_at = datetime.now(UTC).isoformat()
        if allowed_ids is not None:
            atomic_json(
                video.root / "pipeline-batch.json",
                {
                    "started_at": admitted_at,
                    "job_ids": allowed_ids,
                    "max_jobs": max_jobs,
                },
            )

        def consume(stage):
            owner = f"{os.getpid()}:{stage}:{uuid4().hex}"
            while not stopping.is_set():
                with connect_database(config, register_pgvector=False) as guard:
                    guard.autocommit = True
                    if not guard.execute(
                        "SELECT pg_try_advisory_lock(hashtext(%s))",
                        (f"video-consumer:{library_id(video)}:{stage}",),
                    ).fetchone()[0]:
                        stopping.wait(0.5)
                        continue
                    job = claim_stage(config, video, stage, owner, guard, allowed_ids)
                    if not job:
                        stopping.wait(0.5)
                        continue
                    lease = Lease(config, video, job, stage, owner, stopping)
                    heartbeat = threading.Thread(target=lease.heartbeat, daemon=True)
                    heartbeat.start()
                    started = time.monotonic()
                    try:
                        with stage_owner(lease), job_activity(video, job["id"]):
                            lease.before_operation()
                            process(config, video, job, phase=stage)
                            lease.assert_live()
                            # Catch a final output crossing the storage boundary before handoff.
                            if (
                                stage in {"download", "prepare"}
                                and temporary_bytes(config, video)
                                > video.pipeline_temp_bytes
                            ):
                                raise TemporaryBudgetExceeded()
                        finish_stage(
                            config, video, job, stage, owner, time.monotonic() - started
                        )
                    except OwnershipLost:
                        print(
                            json.dumps(
                                {
                                    "job_id": job["id"],
                                    "error_code": "PIPELINE_OWNERSHIP_LOST",
                                }
                            ),
                            flush=True,
                        )
                    except Exception as exc:  # noqa: BLE001 - isolate one durable task
                        fail_stage(
                            config,
                            video,
                            job,
                            stage,
                            owner,
                            exc,
                            time.monotonic() - started,
                        )
                    finally:
                        lease.finished.set()
                        heartbeat.join(timeout=1)
                        guard.execute(
                            "SELECT pg_advisory_unlock(hashtext(%s))",
                            (stage_lock(job["id"], stage),),
                        )

        with ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="video-stage"
        ) as pool:
            futures = [pool.submit(consume, stage) for stage in STAGES]
            try:
                retention_checked = time.monotonic()
                while True:
                    coordinator.execute("SELECT 1")
                    for future in futures:
                        if future.done():
                            future.result()
                            raise RuntimeError("Pipeline consumer unexpectedly stopped")
                    recover_leases(config, video)
                    initialize_jobs(config, video, allowed_ids)
                    if (
                        allowed_ids is None
                        and time.monotonic() - retention_checked >= 300
                    ):
                        from .video_retention import expire_failed_media

                        expire_failed_media(
                            config, video, apply=True, worker_owns_lock=True
                        )
                        retention_checked = time.monotonic()
                    if allowed_ids is not None:
                        with connect_database(config) as c:
                            pending = c.execute(
                                "SELECT count(*) FROM public.rag_video_jobs j WHERE id=ANY(%s::uuid[]) "
                                "AND state NOT IN ('complete','duplicate','blocked','failed') "
                                "AND NOT EXISTS (SELECT 1 FROM public.rag_video_pipeline p WHERE p.job_id=j.id AND p.status='blocked')",
                                (allowed_ids,),
                            ).fetchone()[0]
                        if not pending:
                            break
                    time.sleep(0.5)
            finally:
                stopping.set()
                if max_jobs is not None:
                    set_control(video, paused=True)
        if max_jobs is not None:
            report = {
                "started_at": admitted_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "max_jobs": max_jobs,
                "jobs": [job_status(config, video, j) for j in allowed_ids],
                "pipeline": pipeline_snapshot(config, video),
                "paused": read_control(video).paused,
            }
            atomic_json(video.root / "pipeline-acceptance.json", report)
            return report
