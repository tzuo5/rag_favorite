"""Enable approved timing instrumentation with a guarded worker-only reload."""

import hashlib
import json
import os
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from overnight_monitor import save, service_state

from rag_favorite.config import load_config
from rag_favorite.database import connect_database
from rag_favorite.model_activity import read_calls
from rag_favorite.queue_control import read_control, set_control
from rag_favorite.video_config import load_video_config
from rag_favorite.video_store import library_id


def main():
    os.umask(0o077)
    config = load_config(".runtime/phase1/config.toml")
    video = load_video_config(".runtime/videorag/video.toml")
    output = video.root / "observations" / "overnight-20261011"
    manifest = json.loads((output / "manifest.json").read_text())
    services = service_state()
    worker = "rag-favorite-video-worker.service"
    before_gate = read_control(video)
    original_profile = hashlib.sha256(video.source.read_bytes()).hexdigest()
    before = {
        "at": datetime.now(UTC).isoformat(),
        "gate": asdict(before_gate),
        "services": services,
        "video_profile_sha256": original_profile,
    }
    save(output / "instrumentation-before.json", before)
    set_control(video, paused=True)
    previous_pid = int(services[worker]["MainPID"])
    restarted = False
    try:
        deadline = time.monotonic() + 600
        while True:
            with connect_database(config, register_pgvector=False) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                active_stages = connection.execute(
                    "SELECT count(*) FROM public.rag_video_pipeline WHERE library_id=%s AND status='running'",
                    (library_id(video),),
                ).fetchone()[0]
                active_summaries = connection.execute(
                    "SELECT count(*) FROM public.rag_summary_jobs WHERE library_id=%s AND state='running'",
                    (library_id(video),),
                ).fetchone()[0]
            calls = [
                r
                for r in read_calls(video).values()
                if r["status"] == "running" and Path(f"/proc/{r['pid']}").exists()
            ]
            if not active_stages and not active_summaries and not calls:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Worker drain timed out; queue gate will be restored"
                )
            time.sleep(1)
        if hashlib.sha256(video.source.read_bytes()).hexdigest() != original_profile:
            raise RuntimeError("Runtime profile changed during installation")
        control = {
            "version": 1,
            "enabled": True,
            "run_id": output.name,
            "ends_at_epoch": datetime.fromisoformat(manifest["ends_at"]).timestamp(),
        }
        save(video.root / "observation-control.json", control)
        if services[worker]["ActiveState"] == "active":
            subprocess.run(
                ["systemctl", "--user", "restart", worker], check=True, timeout=45
            )
            restarted = True
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                marker = json.loads((video.root / "queue-worker.json").read_text())
                current = service_state()[worker]
                if (
                    marker.get("pid") == int(current["MainPID"]) != previous_pid
                    and marker.get("ccr_concurrency")
                    == marker.get("llm_consumers")
                    == video.ccr_concurrency
                ):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("Worker did not confirm reloaded profile")
        else:
            marker = {}
        manifest["instrumentation"] = "enabled"
        manifest["instrumentation_started_at"] = datetime.now(UTC).isoformat()
        manifest["instrumented_operations"] = [
            "pipeline_stage_attempt",
            "model_invocation",
            "CCR_HTTP_request",
            "ASR_and_ImageBind_client_request",
            "embedding_batch_request",
            "resource_slot_wait",
            "source_metadata",
            "video_and_image_download",
            "audio_decode",
            "clip_encode",
            "frame_decode",
            "OCR",
            "content_validation",
            "summary_publication",
            "cleanup",
        ]
        save(output / "manifest.json", manifest)
        save(
            output / "instrumentation-deployment.json",
            {
                "before": before,
                "at": manifest["instrumentation_started_at"],
                "worker_marker": marker,
                "reloaded_services": [worker] if restarted else [],
                "video_profile_unchanged": hashlib.sha256(
                    video.source.read_bytes()
                ).hexdigest()
                == original_profile,
            },
        )
        print(
            json.dumps(
                {
                    "instrumentation": "enabled",
                    "worker_marker": marker,
                    "profile_unchanged": True,
                }
            )
        )
    finally:
        if restarted:
            subprocess.run(
                ["systemctl", "--user", "start", worker], check=False, timeout=30
            )
        set_control(video, paused=before_gate.paused)


if __name__ == "__main__":
    main()
