"""Read-only legacy inventory and explicit low-priority, idempotent backfill."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .config import ConfigError
from .database import connect_database
from .video_store import digest_file, enqueue, job_status, library_id, stage_asset

MEDIA = {".mp4", ".mkv", ".mov", ".webm"}
TRANSCRIPTS = (".srt", ".vtt", ".json", ".txt")


def inventory(root: Path) -> dict:
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ConfigError("Backfill source must be a directory.")
    media, notes, transcripts = [], [], []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        suffix = path.suffix.lower()
        relative = str(path.relative_to(root))
        if suffix in MEDIA:
            subtitle = next(
                (
                    path.with_suffix(s)
                    for s in TRANSCRIPTS
                    if path.with_suffix(s).is_file()
                    and not path.with_suffix(s).is_symlink()
                ),
                None,
            )
            media.append(
                {
                    "path": relative,
                    "sha256": digest_file(path),
                    "bytes": path.stat().st_size,
                    "transcript": str(subtitle.relative_to(root)) if subtitle else None,
                    "state": "available",
                }
            )
        elif suffix == ".md":
            content = path.read_text(encoding="utf-8-sig")
            local_refs = [
                value
                for value in re.findall(r"\]\(([^)]+)\)", content)
                if not value.startswith(("http://", "https://"))
                and Path(value).suffix.lower() in MEDIA
            ]
            missing = [
                value for value in local_refs if not (path.parent / value).is_file()
            ]
            notes.append(
                {
                    "path": relative,
                    "sha256": digest_file(path),
                    "state": "source_missing_text_only"
                    if missing
                    else "preserve_manual",
                    "missing_media": missing,
                }
            )
        elif suffix in TRANSCRIPTS:
            if not any(path.with_suffix(s).is_file() for s in MEDIA):
                transcripts.append(
                    {
                        "path": relative,
                        "sha256": digest_file(path),
                        "state": "source_missing_text_only",
                    }
                )
    return {
        "source_root": str(root),
        "videos": media,
        "notes": notes,
        "orphan_transcripts": transcripts,
    }


def backfill(
    config, video, root: Path, collection: str, manifest: Path, *, apply=False
):
    selected = config.collection(collection)
    if apply and selected.read_only:
        raise ConfigError("Selected collection is read-only.")
    current = inventory(root)
    current["collection"] = collection
    receipts = {}
    if manifest.exists():
        previous = json.loads(manifest.read_text())
        if (
            previous["source_root"] != current["source_root"]
            or previous["collection"] != collection
        ):
            raise ConfigError(
                "This backfill manifest belongs to another source or collection."
            )
        receipts = {
            row["sha256"]: row
            for row in previous.get("videos", [])
            if row.get("job_id")
        }
    source_root = Path(current["source_root"])
    if manifest.resolve().is_relative_to(source_root):
        raise ConfigError(
            "Write the backfill manifest outside the legacy source directory."
        )
    for row in current["videos"]:
        if row["sha256"] in receipts:
            receipt = receipts[row["sha256"]]
            state = job_status(config, video, receipt["job_id"])
            row.update(job_id=receipt["job_id"], state=state["state"])
        elif apply:
            subtitle_sha = (
                digest_file(source_root / row["transcript"])
                if row["transcript"]
                else None
            )
            key = hashlib.sha256(
                json.dumps(
                    [str(source_root), collection, row["sha256"], subtitle_sha]
                ).encode()
            ).hexdigest()
            with connect_database(config) as c:
                found = c.execute(
                    "SELECT id,state FROM public.rag_video_jobs WHERE library_id=%s AND collection=%s AND payload->>'submission_key'=%s LIMIT 1",
                    (library_id(video), collection, key),
                ).fetchone()
            if found:
                row.update(job_id=str(found[0]), state=found[1])
                receipts[row["sha256"]] = dict(row)
                save_manifest(manifest, current)
                continue
            asset = stage_asset(source_root / row["path"], video)
            transcript = (
                stage_asset(source_root / row["transcript"], video)
                if row["transcript"]
                else None
            )
            submitted = enqueue(
                config,
                video,
                asset["asset_id"],
                collection,
                transcript["asset_id"] if transcript else None,
                Path(row["path"]).stem,
                priority=0,
                submission_key=key,
            )
            if submitted.get("duplicate"):
                # The duplicate references an earlier source. These fresh copies
                # were never submitted and must not become orphaned media.
                for staged in (asset, transcript):
                    if staged:
                        path = video.root / "assets" / staged["asset_id"]
                        path.unlink(missing_ok=True)
                        path.with_suffix(".json").unlink(missing_ok=True)
            row.update(job_id=submitted["job_id"], state=submitted["state"])
            receipts[row["sha256"]] = dict(row)
            # Persist the receipt after every accepted job so retries cannot
            # submit a completed item again after its owned asset was deleted.
            save_manifest(manifest, current)
    # Manual Markdown and old summaries are never rewritten by the backfill.
    for row in current["notes"]:
        if digest_file(source_root / row["path"]) != row["sha256"]:
            raise ConfigError("A legacy note changed during inventory; retry.")
    save_manifest(manifest, current)
    return current | {"applied": apply}


def save_manifest(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)
