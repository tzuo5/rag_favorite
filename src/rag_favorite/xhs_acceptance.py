"""Local, source-backed acceptance before reopening Xiaohongshu admission."""

from .database import connect_database
from .video_store import library_id
from .xhs_private import note_identity, private_json


def candidates(config, video):
    """Prefer retained evidence of distinct media types; never fetch to classify."""
    from .queue_control import read_control

    with connect_database(config, register_pgvector=False) as db:
        rows = db.execute(
            "SELECT id,payload FROM rag_video_jobs WHERE library_id=%s "
            "AND state IN ('queued','running') AND coalesce(payload->>'asset_id','')='' "
            "AND payload->>'source_url' ~ '^https://www[.]xiaohongshu[.]com/(explore|discovery/item)/' "
            "ORDER BY created_at",
            (library_id(video),),
        ).fetchall()
    paused = set(read_control(video).paused_jobs)
    available = []
    for ident, payload in rows:
        ident = str(ident)
        note_id = note_identity(payload["source_url"])
        if not note_id or ident in paused:
            continue
        path = video.root / "derived" / ident / "download-source.json"
        note = private_json(path).get("note", {}) if path.is_file() else {}
        kind = media_kind(note) if note else "unknown"
        available.append({"job_id": ident, "note_id": note_id, "kind": kind})
    selected, identities = [], set()
    for kind in ("live_photo", "image", "video", "unknown", None):
        for item in available:
            if len(selected) == 3:
                return selected
            if item["note_id"] in identities or (kind and item["kind"] != kind):
                continue
            selected.append(item)
            identities.add(item["note_id"])
            if kind:
                break
    return selected


def media_kind(note):
    if note.get("media_url"):
        return "video"
    if any(item.get("live_video_url") for item in note.get("images", [])):
        return "live_photo"
    return "image" if note.get("images") else "text"


def terminal_failures(config, video, jobs):
    if not jobs:
        return []
    with connect_database(config, register_pgvector=False) as db:
        return [
            {"job_id": str(row[0]), "state": row[1], "error_code": row[2]}
            for row in db.execute(
                "SELECT id,state,error_code FROM rag_video_jobs WHERE library_id=%s "
                "AND id=ANY(%s::uuid[]) AND state IN ('blocked','failed','duplicate')",
                (library_id(video), list(jobs)),
            ).fetchall()
        ]


def verify_publication(config, video, job_id, receipt):
    """Use the same real search and versioned reader as the MCP, to EOF."""
    from .knowledge_summaries import document_context
    from .video_retrieval import search_all

    if not receipt.get("media_validated"):
        return None
    with connect_database(config, register_pgvector=False) as db:
        row = db.execute(
            "SELECT state,payload FROM rag_video_jobs WHERE library_id=%s AND id=%s",
            (library_id(video), job_id),
        ).fetchone()
        if not row or row[0] != "complete":
            return None
        # Require the durable publication stage, not merely a job label.
        published = db.execute(
            "SELECT 1 FROM rag_video_pipeline WHERE job_id=%s AND stage='publish' AND status='done'",
            (job_id,),
        ).fetchone()
    if not published:
        return None
    title = row[1].get("title", "")
    if not title:
        return None
    results = search_all(title, list(config.collections), 5, config, video)
    document_id = "video:" + job_id
    match = next((r for r in results if r["document_id"] == document_id), None)
    if not match:
        return None
    version = match["metadata"]["version"]
    offset, pages = 0, 0
    while True:
        page = document_context(
            document_id,
            list(config.collections),
            config,
            video,
            version=version,
            offset=offset,
            max_characters=16000,
        )
        if not page.get("ok") or page.get("version") != version:
            return None
        pages += 1
        if page.get("complete"):
            return {
                "document_id": document_id,
                "version": version,
                "pages": pages,
                "complete": True,
                "note_id": receipt["note_id"],
                "kind": receipt["kind"],
                "characters": page["total_characters"],
                "media_validated": True,
            }
        next_offset = page.get("next_offset")
        if not isinstance(next_offset, int) or next_offset <= offset:
            return None
        offset = next_offset


def advance(policy, verified):
    """Evidence is produced locally by verify_publication, never by web input."""
    recovery = policy.data["recovery"]
    if recovery["phase"] not in {"acceptance", "observation"}:
        return False
    recovery.setdefault("accepted", {}).update(verified)
    now = policy.clock()
    changed = False
    if recovery["phase"] == "acceptance":
        accepted = recovery["accepted"]
        if len({item["note_id"] for item in accepted.values()}) >= 3:
            recovery.update(
                phase="observation",
                reason=None,
                observation_started_at=now,
                initial_jobs=list(accepted),
                allowed_jobs=[],
            )
            changed = True
    elif now - recovery["observation_started_at"] >= 7200:
        extra = {
            item["note_id"]
            for job, item in recovery["accepted"].items()
            if job not in recovery["initial_jobs"]
        }
        if len(extra) >= 6:
            recovery.update(phase="steady", promoted_at=now, reason=None)
            changed = True
    policy.save()
    return changed
