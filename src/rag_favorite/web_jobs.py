"""Read-only, library-scoped task queries for the responsive web console."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from math import ceil

from psycopg.rows import dict_row

from .database import connect_database
from .queue_control import read_control
from .queue_monitor import PIPELINE_STAGES, SOURCE_ERROR_LABELS, STATES, describe_stage
from .video_store import library_id

DISPLAY_STATES = (*STATES, "paused", "attention")
ORDER = """state_order, CASE WHEN state='queued' THEN priority ELSE 0 END DESC,
    CASE WHEN state='queued' THEN created_at END ASC, updated_at DESC, id"""
BASE = (
    """
WITH tasks AS (
 SELECT j.id, j.collection, j.state, j.stage, j.error_code, j.attempts,
        j.priority, j.created_at, j.updated_at,
        coalesce(nullif(j.payload->>'title',''), nullif(j.payload->>'source_note_id',''),
                 left(j.id::text,12)) AS title,
        coalesce(j.payload->>'media_expired','false')='true' AS media_expired,
        j.id=ANY(%s::uuid[]) AS paused,
        p.stage AS pipeline_stage, p.owner AS stage_owner, p.retry_at,
        CASE WHEN p.status='queued' AND p.retry_at>now() THEN 'retry_wait'
             WHEN p.status='queued' THEN 'waiting' ELSE p.status END AS pipeline_status,
        coalesce(nullif(p.error_code,''),nullif(j.error_code,''),'UNKNOWN') AS reason_code,
        CASE j.state WHEN 'running' THEN 0 WHEN 'published' THEN 1
             WHEN 'queued' THEN 2 WHEN 'blocked' THEN 3 WHEN 'failed' THEN 4 ELSE 5 END AS state_order
 FROM public.rag_video_jobs j
 LEFT JOIN LATERAL (
   SELECT stage,status,owner,retry_at,error_code FROM public.rag_video_pipeline p
   WHERE p.job_id=j.id AND p.library_id=j.library_id AND p.status!='done'
     AND j.state IN ('queued','running','blocked','failed')
   ORDER BY (p.status='running') DESC, p.updated_at DESC,
            CASE p.stage WHEN 'download' THEN 0 WHEN 'prepare' THEN 1 WHEN 'llm' THEN 2 ELSE 3 END
   LIMIT 1
 ) p ON true
 WHERE j.library_id=%s
), displayed AS (
 SELECT *, CASE WHEN state='queued' OR (state='running' AND pipeline_status IN ('waiting','retry_wait'))
                THEN CASE WHEN paused THEN 'paused' ELSE 'queued' END ELSE state END AS display_state
 FROM tasks
), ranked AS (
 SELECT *, CASE WHEN state='queued' AND NOT paused THEN
 count(*) FILTER (WHERE state='queued' AND NOT paused) OVER (ORDER BY """
    + ORDER
    + """ ROWS UNBOUNDED PRECEDING)
 END AS queue_rank FROM displayed
)
"""
)


def reason_label(code):
    return "未确认原因" if code == "UNKNOWN" else SOURCE_ERROR_LABELS.get(code, code)


def _iso(value):
    return value.isoformat() if value else None


def _job(row):
    label, model, progress = describe_stage(row["stage"])
    phase = row["pipeline_stage"]
    if phase in PIPELINE_STAGES:
        label = PIPELINE_STAGES[phase] + " · " + label
    if row["pipeline_status"] == "retry_wait":
        progress = "等待重试"
    result = {
        key: row[key]
        for key in (
            "title",
            "collection",
            "state",
            "stage",
            "error_code",
            "attempts",
            "priority",
            "paused",
            "queue_rank",
            "media_expired",
            "pipeline_stage",
            "pipeline_status",
            "stage_owner",
            "display_state",
            "reason_code",
        )
    }
    result.update(
        id=str(row["id"]),
        stage_label=label,
        model=model,
        progress=progress,
        state_label="已暂停"
        if row["display_state"] == "paused"
        else STATES.get(row["display_state"], row["display_state"]),
        blocked_reason=reason_label(row["reason_code"]),
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
        retry_at=_iso(row["retry_at"]),
    )
    return result


class WebJobQueries:
    def __init__(self, config, video):
        self.config, self.video = config, video

    @contextmanager
    def _read(self):
        control = read_control(self.video)
        with connect_database(self.config, register_pgvector=False) as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            db.execute("SET LOCAL statement_timeout = '5000ms'")
            with db.cursor(row_factory=dict_row) as cursor:
                yield cursor, [list(control.paused_jobs), library_id(self.video)]

    def list_jobs(
        self, *, q="", state="", reason="", collection="", page=1, page_size=40
    ):
        clauses, params = [], []
        if q:
            literal = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append(
                "(title ILIKE %s OR id::text ILIKE %s OR coalesce(error_code,'') ILIKE %s OR nullif(reason_code,'UNKNOWN') ILIKE %s)"
            )
            params.extend(["%" + literal + "%"] * 4)
        if state == "attention":
            clauses.append("display_state IN ('blocked','failed')")
        elif state:
            clauses.append("display_state=%s")
            params.append(state)
        if reason:
            clauses.append("reason_code=%s")
            params.append(reason)
        if collection:
            clauses.append("collection=%s")
            params.append(collection)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._read() as (cursor, base):
            total = cursor.execute(
                BASE + "SELECT count(*) AS total FROM ranked" + where, base + params
            ).fetchone()["total"]
            page = min(page, max(1, ceil(total / page_size)))
            rows = cursor.execute(
                BASE
                + "SELECT * FROM ranked"
                + where
                + " ORDER BY "
                + ORDER
                + " LIMIT %s OFFSET %s",
                base + params + [page_size, (page - 1) * page_size],
            ).fetchall()
        return {
            "items": [_job(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "captured_at": datetime.now(UTC).isoformat(),
        }

    def updates(self, ids):
        with self._read() as (cursor, base):
            rows = cursor.execute(
                BASE + "SELECT * FROM ranked WHERE id=ANY(%s::uuid[])", base + [ids]
            ).fetchall()
        items = [_job(row) for row in rows]
        found = {row["id"] for row in items}
        return {
            "items": items,
            "missing_ids": [value for value in ids if value not in found],
            "captured_at": datetime.now(UTC).isoformat(),
        }

    def get_job(self, job_id):
        result = self.updates([job_id])
        return result["items"][0] if result["items"] else None

    def summary(self):
        with self._read() as (cursor, base):
            counts = {
                row["display_state"]: row["total"]
                for row in cursor.execute(
                    BASE
                    + "SELECT display_state,count(*) AS total FROM ranked GROUP BY display_state",
                    base,
                ).fetchall()
            }
            reasons = cursor.execute(
                BASE
                + "SELECT reason_code AS code,count(*) AS count FROM ranked WHERE display_state IN ('blocked','failed') GROUP BY reason_code ORDER BY count(*) DESC,reason_code",
                base,
            ).fetchall()
            collections = [
                row["collection"]
                for row in cursor.execute(
                    BASE + "SELECT DISTINCT collection FROM ranked ORDER BY collection",
                    base,
                ).fetchall()
            ]
        return {
            "counts": counts,
            "reasons": [{**row, "label": reason_label(row["code"])} for row in reasons],
            "collections": collections,
            "total": sum(counts.values()),
            "captured_at": datetime.now(UTC).isoformat(),
        }
