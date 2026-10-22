"""Drain and reload the existing workers for a CCR concurrency profile change."""

import argparse
import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from rag_favorite.config import load_config
from rag_favorite.database import connect_database
from rag_favorite.model_activity import read_calls
from rag_favorite.queue_control import read_control, set_control
from rag_favorite.video_config import load_video_config
from rag_favorite.video_store import library_id

SERVICES = ("rag-favorite-video-worker.service", "rag-favorite-summary-worker.service")
ROOT = Path(".runtime/ccr-concurrency6-rollout")


def save(name, value):
    path = ROOT / name
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def services():
    result = {}
    for service in SERVICES:
        output = subprocess.check_output(
            [
                "systemctl",
                "--user",
                "show",
                service,
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "MainPID",
            ],
            text=True,
        )
        result[service] = dict(
            line.split("=", 1) for line in output.splitlines() if "=" in line
        )
    return result


def snapshot(config, video):
    with connect_database(config, register_pgvector=False) as connection:
        stages = connection.execute(
            "SELECT stage,count(*) FROM public.rag_video_pipeline WHERE library_id=%s AND status='running' GROUP BY stage",
            (library_id(video),),
        ).fetchall()
        summaries = connection.execute(
            "SELECT count(*) FROM public.rag_summary_jobs WHERE library_id=%s AND state='running'",
            (library_id(video),),
        ).fetchone()[0]
    return {
        "at": datetime.now(UTC).isoformat(),
        "stages": dict(stages),
        "summaries_running": summaries,
        "active_model_calls": sum(
            row.get("status") == "running" and Path(f"/proc/{row.get('pid')}").exists()
            for row in read_calls(video).values()
        ),
        "gate": asdict(read_control(video)),
        "services": services(),
        "ccr_concurrency": video.ccr_concurrency,
    }


def synthetic_smoke(video):
    from rag_favorite import video_provider
    from rag_favorite.pipeline_fence import llm_slot

    if video.ccr_concurrency != 6:
        raise RuntimeError(
            "Smoke test requires the loaded profile to request six slots."
        )
    # Isolated fixtures and library identity; the paused production gate is untouched.
    test_video = replace(
        video,
        root=(ROOT / "synthetic-smoke").resolve(),
        pipeline_enabled=True,
        visual_budget_seconds=60,
    )
    test_video.root.mkdir(mode=0o700, exist_ok=True)
    gate, admitted, lock = (
        threading.Barrier(6, timeout=15),
        threading.Barrier(6, timeout=15),
        threading.Lock(),
    )
    active, peak = 0, 0

    @contextmanager
    def measured_slot(current):
        nonlocal active, peak
        with llm_slot(current):
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                admitted.wait()
                yield
            finally:
                with lock:
                    active -= 1

    original_slot = video_provider.llm_slot
    video_provider.llm_slot = measured_slot

    def request(index):
        provider = video_provider.CCRProvider(test_video)
        provider.start_visual_budget()
        gate.wait()
        expected = {"n": index, "sum": index + 17}
        answer = provider.json(
            "Return only the requested JSON object.",
            f"Calculate {index}+17. Return JSON with n={index} and sum=the answer.",
        )
        return {
            "index": index,
            "correct": answer == expected,
            "diagnostics": provider.calls[-1],
        }

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(request, range(6)))
        report = {
            "requests": 6,
            "correct": sum(r["correct"] for r in results),
            "peak_admitted_by_shared_limiter": peak,
            "content": "synthetic arithmetic only",
            "results": results,
        }
        save("synthetic-smoke.json", report)
        if report["correct"] != 6 or peak != 6:
            raise RuntimeError("Six-concurrency synthetic smoke did not pass.")
        print(
            json.dumps(
                {
                    "synthetic_smoke": "passed",
                    "requests": 6,
                    "peak": peak,
                    "seconds": [r["diagnostics"]["seconds"] for r in results],
                }
            ),
            flush=True,
        )
    finally:
        video_provider.llm_slot = original_slot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("pause", "reload", "status", "smoke"))
    args = parser.parse_args()
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = load_config(".runtime/phase1/config.toml")
    video = load_video_config(".runtime/videorag/video.toml")
    if args.mode == "smoke":
        synthetic_smoke(video)
        return
    if args.mode == "pause":
        if (ROOT / "before-state.json").exists():
            raise RuntimeError(
                "Rollout state already exists; inspect it before starting another rollout."
            )
        before = snapshot(config, video)
        save("before-state.json", before)
        set_control(video, paused=True)
        print(json.dumps({"paused_new_operations": True, "before": before}), flush=True)
        return
    if args.mode == "status":
        value = snapshot(config, video)
        save("current-state.json", value)
        print(json.dumps(value), flush=True)
        return
    before = json.loads((ROOT / "before-state.json").read_text())
    if not read_control(video).paused or video.ccr_concurrency != 6:
        raise RuntimeError(
            "Reload requires the paused queue and an explicit concurrency of 6."
        )
    deadline, previous = time.monotonic() + 1800, None
    while True:
        state = snapshot(config, video)
        active = {
            "stages": state["stages"],
            "summaries": state["summaries_running"],
            "model_calls": state["active_model_calls"],
        }
        if not any(active.values()):
            break
        if active != previous:
            print(json.dumps({"waiting_for_drain": active}), flush=True)
            previous = active
        if time.monotonic() >= deadline:
            set_control(video, paused=before["gate"]["paused"])
            raise RuntimeError(
                "Drain exceeded 30 minutes; previous pause state restored."
            )
        time.sleep(5)
    originally_active = [
        name
        for name, value in before["services"].items()
        if value["ActiveState"] == "active"
    ]
    restarted = False
    try:
        if originally_active:
            subprocess.run(
                ["systemctl", "--user", "stop", *originally_active], check=True
            )
            restarted = True
            state = snapshot(config, video)
            if state["stages"] or state["summaries_running"]:
                raise RuntimeError(
                    "A stage is still marked running after the guarded stop."
                )
            subprocess.run(
                ["systemctl", "--user", "start", *originally_active], check=True
            )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            path = video.root / "queue-worker.json"
            marker = json.loads(path.read_text()) if path.exists() else {}
            current = services()
            video_state = current[SERVICES[0]]
            if marker.get("ccr_concurrency") == marker.get(
                "llm_consumers"
            ) == 6 and marker.get("pid") == int(video_state["MainPID"]):
                break
            if not originally_active:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Worker did not confirm loading six LLM consumers.")
        set_control(video, paused=before["gate"]["paused"])
        after = snapshot(config, video)
        save(
            "deployment.json",
            {
                "before": before,
                "after": after,
                "worker_marker": marker,
                "services_reloaded": originally_active,
                "observed_six_simultaneous_production_requests": False,
            },
        )
        print(
            json.dumps(
                {"deployment": "complete", "after": after, "worker_marker": marker}
            ),
            flush=True,
        )
    finally:
        # Keep pre-existing service and queue state if a reload step fails.
        if restarted:
            subprocess.run(
                ["systemctl", "--user", "start", *originally_active], check=False
            )
        if read_control(video).paused != before["gate"]["paused"]:
            set_control(video, paused=before["gate"]["paused"])


if __name__ == "__main__":
    main()
