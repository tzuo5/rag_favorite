"""Real PG/filesystem failure drills; replay saved synthetic extraction, no new inference."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from rag_favorite import video_worker
from rag_favorite.config import ConfigError, database_credentials, load_config
from rag_favorite.database import connect_database
from rag_favorite.video_backfill import backfill
from rag_favorite.video_config import load_video_config
from rag_favorite.video_provider import ProviderUnavailable
from rag_favorite.video_retention import (
    attach_transcript,
    expire_failed_media,
    retry_job,
)
from rag_favorite.video_store import (
    digest_file,
    enqueue,
    enqueue_url,
    job_status,
    library_id,
    publish,
    stage_asset,
    update_job,
)


def main():
    config, active_video = load_config(), load_video_config()
    if not database_credentials(config)["name"].startswith("rag_phase1"):
        raise ConfigError("Failure drills require the isolated rag_phase1 database.")
    repo = Path(__file__).resolve().parents[1]
    root = repo / ".runtime/review-20261004" / ("recovery-" + uuid4().hex)
    root.mkdir(parents=True, mode=0o700)
    video = replace(active_video, root=root / "owned")
    fixture_video = replace(
        active_video, root=active_video.root.parent / "test-fixtures"
    )
    fixtures = fixture_video.root / "benchmark"
    original = root / "legacy"
    original.mkdir()
    source = original / "sample.mp4"
    shutil.copyfile(fixtures / "synthetic.mp4", source)
    shutil.copyfile(fixtures / "cooking.srt", source.with_suffix(".srt"))
    manual = original / "manual.md"
    manual.write_text("人工正文与旧摘要\n[已缺失视频](missing.mp4)\n")
    initial_hashes = {p.name: digest_file(p) for p in original.iterdir()}
    template = json.loads(
        (
            fixture_video.root
            / "derived/844563fd-bf1d-4730-b453-9b7a0dbf1b08/record.json"
        ).read_text()
    )
    outcomes = []

    def queued():
        asset = stage_asset(source, video)
        submitted = enqueue(
            config, video, asset["asset_id"], "cooking", title="Recovery fixture"
        )
        return {
            "id": submitted["job_id"],
            "collection": "cooking",
            "state": "published",
            "payload": {
                "asset_id": asset["asset_id"],
                "title": "Recovery fixture",
                "source_label": source.name,
                "sha256": asset["sha256"],
            },
        }

    def prepared(job):
        record = copy.deepcopy(template)
        record["id"] = job["id"]
        derived = video.root / "derived" / job["id"]
        derived.mkdir(parents=True)
        for segment in record["segments"]:
            segment["id"] = job["id"] + ":" + str(segment["ordinal"])
            for image in segment["evidence"]:
                old = fixture_video.root / image["relative_path"]
                new = derived / old.name
                shutil.copyfile(old, new)
                image["relative_path"] = str(new.relative_to(video.root))
                image["id"] = segment["id"] + ":image:" + image["id"].rsplit(":", 1)[-1]
        video_worker.atomic_json(derived / "record.json", record)
        publish(config, video, job, record, record["segments"])
        return record

    try:
        first = backfill(
            config, video, original, "cooking", root / "manifest.json", apply=True
        )
        # Lose the file receipt, as if the process died after enqueue committed.
        (root / "manifest.json").unlink()
        second = backfill(
            config, video, original, "cooking", root / "manifest.json", apply=True
        )
        assert first["videos"][0]["job_id"] == second["videos"][0]["job_id"]
        low_job = first["videos"][0]["job_id"]
        high_job = queued()
        update_job(config, video, high_job["id"], "running", "simulated_interruption")
        order = []

        def record_order(config, video, job):
            order.append(str(job["id"]))
            update_job(config, video, str(job["id"]), "blocked", "drill_completed")

        with (
            patch.object(video_worker, "local_encoder", return_value={}),
            patch.object(video_worker, "process", side_effect=record_order),
        ):
            video_worker.run_worker(config, video, once=True)
            video_worker.run_worker(config, video, once=True)
        assert order == [high_job["id"], low_job]
        outcomes.append(
            {
                "case": "restart_and_priority",
                "passed": True,
                "recovered_running_first": True,
            }
        )
        outcomes.append(
            {"case": "backfill_lost_receipt", "passed": True, "duplicate_jobs": 0}
        )

        before_cleanup = queued()
        prepared(before_cleanup)

        # New process resumes the durable published checkpoint without an encoder.
        def encoder_offline(*_):
            raise ProviderUnavailable("Drill: encoder offline.")

        with patch.object(video_worker, "local_encoder", side_effect=encoder_offline):
            video_worker.run_worker(config, video, once=True)
        assert job_status(config, video, before_cleanup["id"])["state"] == "complete"
        outcomes.append({"case": "published_cleanup_encoder_offline", "passed": True})

        crash = queued()
        prepared(crash)
        task = root / "crash-task.json"
        task.write_text(json.dumps({"root": str(video.root), "job": crash}))
        task.chmod(0o600)
        child_code = """
import json, os, sys
from pathlib import Path
from dataclasses import replace
from rag_favorite.config import load_config
from rag_favorite.video_config import load_video_config
from rag_favorite import video_worker
d=json.loads(Path(sys.argv[1]).read_text())
v=replace(load_video_config(),root=Path(d['root']))
video_worker.finish_cleanup=lambda *_: os._exit(24)
video_worker.cleanup(load_config(),v,d['job'])
"""
        child = subprocess.run(
            [sys.executable, "-c", child_code, str(task)],
            check=False,
            capture_output=True,
        )
        assert child.returncode == 24
        assert not (video.root / "assets" / crash["payload"]["asset_id"]).exists()
        assert job_status(config, video, crash["id"])["state"] == "published"
        with patch.object(video_worker, "local_encoder", side_effect=encoder_offline):
            video_worker.run_worker(config, video, once=True)
        assert job_status(config, video, crash["id"])["state"] == "complete"
        outcomes.append(
            {"case": "process_exit_after_delete_before_commit", "passed": True}
        )

        corrupt = queued()
        record = prepared(corrupt)
        image = video.root / record["segments"][0]["evidence"][0]["relative_path"]
        saved = image.read_bytes()
        image.write_bytes(b"corrupt")
        try:
            video_worker.cleanup(config, video, corrupt)
            raise AssertionError("Corrupt evidence did not block cleanup")
        except RuntimeError:
            assert (video.root / "assets" / corrupt["payload"]["asset_id"]).is_file()
        image.write_bytes(saved)
        video_worker.cleanup(config, video, corrupt)
        outcomes.append({"case": "corrupt_evidence_retains_source", "passed": True})

        expired, recent, queued_job = queued(), queued(), queued()
        for job in (expired, recent):
            update_job(
                config, video, job["id"], "failed", "extract:0", "INJECTED_FAILURE"
            )
        work = video.root / "work" / expired["id"]
        work.mkdir(parents=True)
        video_worker.atomic_json(
            work / "transcript.json", {"segments": [{"text": "可恢复转录"}]}
        )
        (work / "audio.wav").write_bytes(b"temporary audio")
        with connect_database(config) as c:
            c.execute(
                "UPDATE public.rag_video_jobs SET updated_at=now()-interval '73 hours' WHERE id=ANY(%s)",
                ([expired["id"], queued_job["id"]],),
            )
        preview = expire_failed_media(config, video)
        assert [r["job_id"] for r in preview["jobs"]] == [expired["id"]]
        expire_failed_media(config, video, apply=True)
        assert not (video.root / "assets" / expired["payload"]["asset_id"]).exists()
        assert (
            video.root / "derived" / expired["id"] / "recovered-transcript.json"
        ).is_file()
        assert (video.root / "assets" / recent["payload"]["asset_id"]).is_file()
        assert (video.root / "assets" / queued_job["payload"]["asset_id"]).is_file()
        try:
            retry_job(config, video, expired["id"])
            raise AssertionError("Expired task retried without source")
        except ConfigError:
            pass
        outcomes.append({"case": "failed_ttl_and_active_protection", "passed": True})
        subtitle = stage_asset(source.with_suffix(".srt"), video)
        attach_transcript(config, video, recent["id"], subtitle["asset_id"])
        assert job_status(config, video, recent["id"])["state"] == "queued"
        with connect_database(config) as c:
            payload = c.execute(
                "SELECT payload FROM public.rag_video_jobs WHERE id=%s", (recent["id"],)
            ).fetchone()[0]
        assert payload["transcript_asset_id"] == subtitle["asset_id"]
        try:
            attach_transcript(config, video, expired["id"], subtitle["asset_id"])
            raise AssertionError("Expired source accepted a transcript")
        except ConfigError:
            pass
        outcomes.append(
            {"case": "attach_transcript_without_redownload", "passed": True}
        )
        link = "https://xhslink.cn/o/RecoveryFixture"
        old_url = enqueue_url(config, video, link, "cooking")
        with connect_database(config) as c:
            c.execute(
                "UPDATE public.rag_video_jobs SET state='blocked',stage='media_expired',payload=jsonb_set(payload,'{media_expired}','true') WHERE id=%s",
                (old_url["job_id"],),
            )
        new_url = enqueue_url(config, video, link, "cooking")
        duplicate = enqueue_url(config, video, link, "cooking")
        assert (
            new_url["job_id"] != old_url["job_id"]
            and duplicate["job_id"] == new_url["job_id"]
        )
        outcomes.append({"case": "explicit_resubmit_after_expiry", "passed": True})
        assert initial_hashes == {p.name: digest_file(p) for p in original.iterdir()}
        outcomes.append(
            {"case": "manual_and_external_sources_unchanged", "passed": True}
        )
    finally:
        with connect_database(config) as c:
            c.execute(
                "DELETE FROM public.rag_videos WHERE library_id=%s",
                (library_id(video),),
            )
            c.execute(
                "DELETE FROM public.rag_video_jobs WHERE library_id=%s",
                (library_id(video),),
            )
        shutil.rmtree(video.root, ignore_errors=True)
    result = {
        "date": "2026-10-04",
        "scope": "real PG/filesystem; synthetic extraction replay; separate library; no inference quality claim",
        "live_library_modified": False,
        "scratch_rows_removed": True,
        "cases": outcomes,
    }
    (repo / "docs/implementation/video-recovery-results.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
