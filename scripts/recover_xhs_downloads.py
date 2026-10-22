"""Audit/recover blocked XHS sources and probe readiness without resuming tasks.

All manifests contain private source URLs and must remain owner-only.
--apply classifies blocked tasks without requeueing them. --probe-job validates
at most two sources without releasing the source gate or triggering inference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from psycopg.types.json import Jsonb

from rag_favorite.config import load_config
from rag_favorite.database import connect_database
from rag_favorite.video_config import load_video_config
from rag_favorite.video_provider import ProviderUnavailable
from rag_favorite.video_sources import fetch_note
from rag_favorite.video_store import library_id
from rag_favorite.xhs_client import XhsSessionClient
from rag_favorite.xhs_private import (
    atomic_private_json,
    private_directory,
    private_json,
)
from rag_favorite.xhs_recovery import collect_candidates, recover_payload


def recover(config, video, manifest, *, apply=False):
    lid = library_id(video)
    candidates = {}
    with connect_database(config, register_pgvector=False) as c:
        c.execute("SET TRANSACTION READ ONLY")
        sources = c.execute(
            "SELECT payload FROM rag_video_jobs WHERE library_id=%s ORDER BY updated_at DESC",
            (lid,),
        ).fetchall()
        sources += c.execute(
            "SELECT previous_payload FROM rag_title_job_aliases WHERE library_id=%s ORDER BY created_at DESC",
            (lid,),
        ).fetchall()
        sources += c.execute(
            "SELECT metadata FROM rag_knowledge_documents WHERE library_id=%s", (lid,)
        ).fetchall()
        for (value,) in sources:
            collect_candidates(value, candidates)
        rows = c.execute(
            "SELECT id,payload,stage,error_code,priority FROM rag_video_jobs WHERE library_id=%s AND state='blocked' AND stage='failed:download' AND error_code='ProviderUnavailable' AND coalesce(payload->>'source_url','') ~ '^https://(www[.]xiaohongshu[.]com|xhslink[.]cn)/' ORDER BY id",
            (lid,),
        ).fetchall()
    report = {"at": datetime.now(UTC).isoformat(), "applied": False, "jobs": []}
    for jid, payload, stage, code, priority in rows:
        updated, category, restored = recover_payload(payload, candidates)
        report["jobs"].append(
            {
                "id": str(jid),
                "original_payload": payload,
                "original_stage": stage,
                "original_error": code,
                "original_priority": priority,
                "updated_payload": updated,
                "category": category,
                "restored": restored,
            }
        )
    if apply:
        if manifest.exists():
            raise RuntimeError(
                "Use a new manifest path; never overwrite recovery preimages"
            )
        private_directory(manifest.parent)
        atomic_private_json(manifest, report)  # Durable preimage precedes any mutation.
        with connect_database(config, register_pgvector=False) as c:
            gate = c.execute(
                "SELECT xhs_login_required FROM rag_video_pipeline_control WHERE library_id=%s",
                (lid,),
            ).fetchone()
            if not gate or not gate[0]:
                raise RuntimeError(
                    "Enable the XHS source gate before applying recovery"
                )
            for entry in report["jobs"]:
                error = (
                    "XHS_SOURCE_URL_REQUIRED"
                    if entry["category"] == "needs_source_url"
                    else "XHS_SOURCE_VALIDATION_REQUIRED"
                )
                changed = c.execute(
                    "UPDATE rag_video_jobs SET payload=%s,error_code=%s,updated_at=now() WHERE id=%s AND library_id=%s AND state='blocked' AND error_code='ProviderUnavailable' AND payload=%s",
                    (
                        Jsonb(entry["updated_payload"]),
                        error,
                        entry["id"],
                        lid,
                        Jsonb(entry["original_payload"]),
                    ),
                ).rowcount
                if changed:
                    c.execute(
                        "UPDATE rag_video_pipeline SET error_code=%s,updated_at=now() WHERE job_id=%s AND stage='download' AND status='blocked'",
                        (error, entry["id"]),
                    )
                entry["applied"] = bool(changed)
        report["applied"] = True
        atomic_private_json(manifest, report)
    return {
        "jobs": len(rows),
        "categories": dict(Counter(e["category"] for e in report["jobs"])),
        "restored": sum(e["restored"] for e in report["jobs"]),
        "applied": report["applied"],
    }


def probe_sources(config, video, ids):
    ids = list(dict.fromkeys(str(UUID(j)) for j in ids))
    if not 1 <= len(ids) <= 2:
        raise ValueError("Probe one or two acceptance jobs first")
    owner = XhsSessionClient(video).owner()
    binding = private_json(video.root / "favorites" / "owner.json")
    if binding.get("owner_hash") != hashlib.sha256(owner.encode()).hexdigest():
        raise ProviderUnavailable("XHS_ACCOUNT_CHANGED_RESTART_REQUIRED")
    with connect_database(config, register_pgvector=False) as c:
        rows = c.execute(
            "SELECT id,payload FROM rag_video_jobs WHERE library_id=%s AND id=ANY(%s::uuid[]) AND state='blocked' AND stage='failed:download'",
            (library_id(video), ids),
        ).fetchall()
    if len(rows) != len(ids):
        raise ValueError("Acceptance jobs must be blocked downloads in this library")
    for _, payload in rows:
        fetch_note(
            video, payload["source_url"]
        )  # Does not invoke models or download media.
    return {"source_verified": ids, "completed": False, "source_gate_released": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--probe-job", action="append", default=[])
    args = parser.parse_args()
    if args.probe_job and args.apply:
        parser.error("Use classification and probing separately")
    if args.apply and not args.manifest:
        parser.error("--apply requires --manifest")
    config, video = load_config(), load_video_config()
    try:
        result = (
            probe_sources(config, video, args.probe_job)
            if args.probe_job
            else recover(config, video, args.manifest, apply=args.apply)
        )
        print(json.dumps(result, ensure_ascii=False))
    except ProviderUnavailable as exc:
        print(
            json.dumps(
                {"error_code": exc.code or type(exc).__name__, "completed": False}
            )
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
