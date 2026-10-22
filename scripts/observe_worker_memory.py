"""Read-only, timed PSS/cgroup acceptance for the deployed video worker."""

import argparse
import json
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from rag_favorite.config import load_config
from rag_favorite.database import connect_database
from rag_favorite.queue_control import read_control
from rag_favorite.video_config import load_video_config
from rag_favorite.video_store import library_id


def sample(config, video):
    output = subprocess.check_output(
        [
            "systemctl",
            "--user",
            "show",
            "rag-favorite-video-worker.service",
            "-p",
            "MainPID",
            "-p",
            "ActiveState",
            "-p",
            "ControlGroup",
            "-p",
            "NRestarts",
        ],
        text=True,
    )
    service = dict(line.split("=", 1) for line in output.splitlines())
    group = Path("/sys/fs/cgroup") / service["ControlGroup"].lstrip("/")
    pids = set()
    for path in group.rglob("cgroup.procs"):
        pids.update(path.read_text().split())
    pss = 0
    for pid in pids:
        try:
            lines = Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines()
            pss += int(
                next(line.split()[1] for line in lines if line.startswith("Pss:"))
            )
        except FileNotFoundError:
            continue
    with connect_database(config, register_pgvector=False) as c:
        stages = dict(
            c.execute(
                "SELECT stage,count(*) FROM public.rag_video_pipeline WHERE library_id=%s AND status='running' GROUP BY stage",
                (library_id(video),),
            ).fetchall()
        )
        jobs = dict(
            c.execute(
                "SELECT state,count(*) FROM public.rag_video_jobs WHERE library_id=%s GROUP BY state",
                (library_id(video),),
            ).fetchall()
        )
        gate = c.execute(
            "SELECT xhs_download_blocked,xhs_login_required FROM public.rag_video_pipeline_control WHERE library_id=%s",
            (library_id(video),),
        ).fetchone()
    return {
        "at": datetime.now(UTC).isoformat(),
        "service": service,
        "pids": sorted(pids),
        "pss_kib": pss,
        "memory_current": int((group / "memory.current").read_text()),
        "memory_events": {
            key: int(value)
            for key, value in (
                line.split()
                for line in (group / "memory.events").read_text().splitlines()
            )
        },
        "stages_running": stages,
        "jobs": jobs,
        "source_gate": gate,
        "paused": read_control(video).paused,
        "ccr_concurrency": video.ccr_concurrency,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--video-config", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=3600)
    parser.add_argument("--warmup", type=int, default=600)
    args = parser.parse_args()
    config, video = load_config(args.config), load_video_config(args.video_config)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    started, rows = time.monotonic(), []
    with (args.output / "samples.jsonl").open("x") as log:
        while True:
            row = sample(config, video)
            row["elapsed"] = time.monotonic() - started
            rows.append(row)
            log.write(json.dumps(row) + "\n")
            log.flush()
            if row["elapsed"] >= args.seconds:
                break
            time.sleep(min(20, args.seconds - row["elapsed"]))
    warm = [row for row in rows if row["elapsed"] >= args.warmup]
    idle = all(not row["stages_running"] for row in warm)
    delta = (warm[-1]["pss_kib"] - warm[0]["pss_kib"]) / 1024
    slope = statistics.linear_regression(
        [row["elapsed"] / 60 for row in warm],
        [row["pss_kib"] / 1024 for row in warm],
    ).slope
    stable_pid = len({row["service"]["MainPID"] for row in rows}) == 1
    events = {
        key: rows[-1]["memory_events"][key] - rows[0]["memory_events"][key]
        for key in ("high", "oom", "oom_kill")
    }
    report = {
        "elapsed_seconds": rows[-1]["elapsed"],
        "samples": len(rows),
        "warmup_seconds": args.warmup,
        "idle": idle,
        "stable_pid": stable_pid,
        "pss_growth_mib_after_warmup": delta,
        "pss_slope_mib_per_minute": slope,
        "max_pss_mib": max(row["pss_kib"] for row in rows) / 1024,
        "memory_events_delta": events,
        "all_samples_concurrency_10": all(row["ccr_concurrency"] == 10 for row in rows),
        # Small allocator/PSS noise is tolerated; sustained > 6 MiB/hour is not.
        "accepted": idle
        and stable_pid
        and delta < 50
        and slope < 0.1
        and not any(events.values())
        and rows[-1]["elapsed"] >= 3600,
        "real_tasks_completed_during_observation": rows[-1]["jobs"].get("complete", 0)
        - rows[0]["jobs"].get("complete", 0),
    }
    (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)
    if not report["accepted"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
