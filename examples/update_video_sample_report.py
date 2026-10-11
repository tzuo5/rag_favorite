"""Refresh the private original-input manifest and a secret-free MVP report."""

from __future__ import annotations

import json
from pathlib import Path

from rag_favorite.config import load_config
from rag_favorite.database import connect_database
from rag_favorite.video_config import load_video_config
from rag_favorite.video_store import job_status, library_id


def main():
    repo = Path(__file__).resolve().parents[1]
    manifest_path = repo / ".runtime/videorag/samples/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    config, video = load_config(), load_video_config()
    summary = []
    for row in manifest["samples"]:
        state = job_status(config, video, row["job_id"])
        row.update(
            state=state["state"], stage=state["stage"], error_code=state["error_code"]
        )
        source = video.root / "derived" / row["job_id"] / "source.json"
        metadata = json.loads(source.read_text()) if source.exists() else {}
        row["content_type"] = metadata.get("content_type", "unverified")
        saved = video.root / "derived" / row["job_id"] / "record.json"
        record = json.loads(saved.read_text()) if saved.exists() else {}
        with connect_database(config) as c:
            published = c.execute(
                "SELECT source_deleted,classification FROM public.rag_videos WHERE id=%s AND library_id=%s",
                (row["job_id"], library_id(video)),
            ).fetchone()
            payload = c.execute(
                "SELECT payload FROM public.rag_video_jobs WHERE id=%s",
                (row["job_id"],),
            ).fetchone()[0]
        row["classification"] = published[1] if published else "pending"
        owned = [
            payload.get("asset_id"),
            payload.get("transcript_asset_id"),
            *payload.get("superseded_asset_ids", []),
        ]
        remaining = sum(
            (video.root / "assets" / asset).exists() for asset in owned if asset
        )
        summary.append(
            {
                "sample_id": row["id"],
                "job_id": row["job_id"],
                "collection": row["collection"],
                "state": state["state"],
                "stage": state["stage"],
                "content_type": row["content_type"],
                "source_deleted": published[0] if published else None,
                "owned_assets_remaining": remaining,
                "work_directory_remaining": (
                    video.root / "work" / row["job_id"]
                ).exists(),
                "segments": len(record.get("segments", [])),
                "visual_executed": record.get("visual_executed", False),
                "facts": sum(len(s["facts"]) for s in record.get("segments", [])),
                "images": sum(len(s["evidence"]) for s in record.get("segments", [])),
                "error_code": state["error_code"],
                "author_note_indexed": payload.get("author_note_indexed", False),
            }
        )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    manifest_path.chmod(0o600)
    result = {
        "date": "2026-10-04",
        "scope": "real_owner_provided_inputs_not_quality_gold",
        "total": len(summary),
        "complete": sum(r["state"] == "complete" for r in summary),
        "quality_acceptance": "not_verified",
        "samples": summary,
    }
    (repo / "docs/implementation/real-sample-ingestion-results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "samples"}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
