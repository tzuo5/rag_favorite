"""OOM/restart/fencing drill in an isolated service and disposable test database.

Only synthetic checkpoints are used. No model or source-media calls are made.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from rag_favorite import video_pipeline as pipeline
from rag_favorite.config import database_credentials, load_config
from rag_favorite.database import connect_database
from rag_favorite.pipeline_fence import OwnershipLost
from rag_favorite.video_config import VideoConfig
from rag_favorite.video_store import library_id
from rag_favorite.video_worker import atomic_json, compatible_checkpoint


def worker(config, video, root):
    # Accelerate only this disposable fixture's lease; production stays at 120s.
    pipeline.LEASE_SECONDS = 3
    starts = root / "starts.json"
    history = json.loads(starts.read_text()) if starts.exists() else []
    checkpoint = root / "segment.json"
    previous_alive = []
    for row in history:
        for pid in row["pids"]:
            stat = Path(f"/proc/{pid}/stat")
            if stat.exists() and stat.read_text().split(")", 1)[1].split()[0] != "Z":
                previous_alive.append(pid)
    assert not previous_alive, previous_alive
    guard = connect_database(config, register_pgvector=False)
    guard.autocommit = True
    recovery = None
    if len(history) < 2:
        recovered = pipeline.recover_leases(config, video)
        owner = f"oom-fixture:{os.getpid()}"
        job = pipeline.claim_stage(config, video, "llm", owner, guard)
        assert job
        if checkpoint.exists():
            saved = json.loads(checkpoint.read_text())
            assert compatible_checkpoint(saved, "synthetic-v1", "unused", visual=False)
            stale_rejected = False
            try:
                pipeline.finish_stage(
                    config, video, saved["job"], "llm", saved["owner"], 0.1
                )
            except OwnershipLost:
                stale_rejected = True
            assert (
                stale_rejected
                and recovered == 1
                and job["token"] > saved["job"]["token"]
            )
            pipeline.finish_stage(config, video, job, "llm", owner, 0.1)
            with connect_database(config, register_pgvector=False) as c:
                next_stage = c.execute(
                    "SELECT status FROM public.rag_video_pipeline WHERE job_id=%s AND stage='publish'",
                    (job["id"],),
                ).fetchone()[0]
            recovery = {
                "lease_recovered": recovered,
                "checkpoint_reused": True,
                "stale_owner_rejected": stale_rejected,
                "new_token": job["token"],
                "previous_token": saved["job"]["token"],
                "publish_status": next_stage,
            }
        else:
            atomic_json(
                checkpoint,
                {
                    "cache_key": "synthetic-v1",
                    "segment": {},
                    "job": job,
                    "owner": owner,
                },
            )
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    # Wait briefly before allocating so the durable process list is recorded.
    balloon = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(1); chunks=[]\nwhile True: chunks.append(bytearray(8*1024*1024)); time.sleep(.03)",
        ]
    )
    history.append(
        {
            "at": time.time(),
            "pids": [os.getpid(), sentinel.pid, balloon.pid],
            "previous_processes_alive": previous_alive,
            "recovery": recovery,
        }
    )
    atomic_json(starts, history)
    while True:
        time.sleep(1)


def state(unit):
    output = subprocess.check_output(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "MainPID",
            "-p",
            "Result",
            "-p",
            "NRestarts",
            "-p",
            "ControlGroup",
        ],
        text=True,
    )
    result = dict(line.split("=", 1) for line in output.splitlines())
    group = Path("/sys/fs/cgroup") / result["ControlGroup"].lstrip("/")
    if result["ControlGroup"] and (group / "memory.events").exists():
        result["events"] = (group / "memory.events").read_text()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    config = load_config(args.config)
    assert database_credentials(config)["name"].startswith("rag_memory_test_"), (
        "Requires a disposable memory-test database"
    )
    root = args.root.resolve()
    video = VideoConfig(
        root=root / "library",
        credentials_file=root / "unused",
        pipeline_enabled=True,
        ccr_concurrency=10,
    )
    if args.worker:
        worker(config, video, root)
        return
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    video.root.mkdir()
    job_id, unit = str(uuid4()), "rag-memory-oom-test-" + uuid4().hex[:10] + ".service"
    with connect_database(config, register_pgvector=False) as c:
        c.execute(
            "INSERT INTO public.rag_video_jobs(id,library_id,collection,state,stage,payload) VALUES(%s,%s,'general','running','llm','{\"title\":\"synthetic OOM drill\"}')",
            (job_id, library_id(video)),
        )
        c.execute(
            "INSERT INTO public.rag_video_pipeline(job_id,library_id,stage,status) VALUES(%s,%s,'llm','queued')",
            (job_id, library_id(video)),
        )
    command = ["systemd-run", "--user", "--unit=" + unit]
    for prop in [
        "MemoryHigh=127M",
        "MemoryMax=128M",
        "MemorySwapMax=16M",
        "OOMPolicy=kill",
        "KillMode=control-group",
        "Restart=on-failure",
        "RestartSec=20s",
        "StartLimitIntervalSec=10min",
        "StartLimitBurst=3",
        "UMask=0077",
    ]:
        command.append("--property=" + prop)
    command += [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--config",
        str(Path(args.config).resolve()),
        "--root",
        str(root),
    ]
    observed = []
    try:
        subprocess.run(command, check=True)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            current = state(unit)
            if not observed or current != observed[-1]:
                observed.append(current)
            if current["ActiveState"] == "failed":
                break
            time.sleep(0.5)
        starts = json.loads((root / "starts.json").read_text())
        journal = subprocess.check_output(
            ["journalctl", "--user", "-u", unit, "--no-pager", "-o", "cat"], text=True
        )
        (root / "journal.log").write_text(journal)
        assert len(starts) == 3, starts
        # systemd 259 can retain oom-kill as Result after refusing the fourth
        # start. Require the journal's explicit rate-limit rejection as well.
        assert current["ActiveState"] == "failed", current
        assert current["Result"] in {"start-limit-hit", "oom-kill"}, current
        assert "Start request repeated too quickly" in journal, journal
        assert sum(row["Result"] == "oom-kill" for row in observed) >= 3
        assert starts[1]["recovery"]["checkpoint_reused"]
        assert starts[1]["recovery"]["publish_status"] == "queued"
        assert all(
            not Path(f"/proc/{pid}").exists()
            or Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[0] == "Z"
            for row in starts
            for pid in row["pids"]
        )
        result = {
            "passed": True,
            "starts": starts,
            "final": current,
            "observed": observed,
            "production_service_oom_triggered": False,
            "unit": unit,
            "restart_delays_seconds": [
                starts[i]["at"] - starts[i - 1]["at"] for i in range(1, len(starts))
            ],
        }
        atomic_json(root / "result.json", result)
        print(
            json.dumps(
                {key: value for key, value in result.items() if key != "observed"}
            ),
            flush=True,
        )
    finally:
        subprocess.run(["systemctl", "--user", "stop", unit], check=False)
        subprocess.run(["systemctl", "--user", "reset-failed", unit], check=False)
        with connect_database(config, register_pgvector=False) as c:
            c.execute("DELETE FROM public.rag_video_jobs WHERE id=%s", (job_id,))


if __name__ == "__main__":
    main()
