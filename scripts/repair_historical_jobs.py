"""Explicit, checkpoint-preserving recovery of the observed blocked/failed jobs.

The private manifest is written before mutations and is reused on reruns.
Requeued jobs are reported as queued, never as completed. Run with production
config variables and --apply only after validating the runtime fixes.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from rag_favorite.config import load_config
from rag_favorite.database import connect_database
from rag_favorite.queue_control import read_control
from rag_favorite.video_config import load_video_config
from rag_favorite.video_retention import retry_job
from rag_favorite.video_sources import validate_download
from rag_favorite.video_store import asset_path, library_id


def private_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.touch(mode=0o600)
    temporary.chmod(0o600)
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def recover(config, video, manifest, *, apply=False):
    if manifest.exists():
        report = json.loads(manifest.read_text())
    else:
        with connect_database(config) as db:
            rows = db.execute(
                "SELECT id,state,stage,error_code,priority FROM public.rag_video_jobs WHERE library_id=%s AND state IN ('failed','blocked') ORDER BY created_at",
                (library_id(video),),
            ).fetchall()
        report = {
            "created_at": datetime.now(UTC).isoformat(),
            "jobs": [
                {
                    "id": str(r[0]),
                    "original_state": r[1],
                    "original_stage": r[2],
                    "original_error": r[3],
                    "original_priority": r[4],
                }
                for r in rows
            ],
        }
        private_write(manifest, report)
    if apply and not read_control(video).paused:
        raise RuntimeError("Pause claims before applying recovery")
    for entry in report["jobs"]:
        with connect_database(config) as db:
            row = db.execute(
                "SELECT state,payload FROM public.rag_video_jobs WHERE id=%s AND library_id=%s",
                (entry["id"], library_id(video)),
            ).fetchone()
        if not row:
            entry["recovery"] = "missing_job"
            continue
        state, payload = row
        if state not in {"failed", "blocked"}:
            entry["recovery"] = state
            continue
        redownload = False
        if (
            payload.get("asset_id")
            and (video.root / "assets" / payload["asset_id"]).is_file()
        ):
            try:
                validate_download(video, asset_path(video, payload["asset_id"])[0])
            except Exception as exc:  # noqa: BLE001 - never print source URLs or credentials
                entry["media_error"] = getattr(exc, "code", None) or type(exc).__name__
                redownload = True
        entry["redownload"] = redownload or bool(payload.get("media_expired"))
        if apply:
            try:
                retry_job(config, video, entry["id"], redownload=redownload)
                with connect_database(config) as db:
                    db.execute(
                        "UPDATE public.rag_video_jobs SET priority=1000 WHERE id=%s AND library_id=%s AND state='queued'",
                        (entry["id"], library_id(video)),
                    )
                entry["recovery"] = "queued"
            except Exception as exc:  # noqa: BLE001 - persist only symbolic diagnostics
                entry["recovery"] = "blocked"
                entry["recovery_error"] = (
                    getattr(exc, "code", None) or type(exc).__name__
                )
        private_write(manifest, report)
    report["updated_at"] = datetime.now(UTC).isoformat()
    private_write(manifest, report)
    return {
        "jobs": len(report["jobs"]),
        "states": {
            state: sum(j.get("recovery") == state for j in report["jobs"])
            for state in sorted(
                {j.get("recovery", "inspected") for j in report["jobs"]}
            )
        },
        "redownload": sum(j.get("redownload", False) for j in report["jobs"]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            recover(load_config(), load_video_config(), args.manifest, apply=args.apply)
        )
    )
