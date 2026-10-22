"""Exercise ten real stage consumers using local synthetic work, without models."""

import argparse
import json
import os
import threading
import time
from pathlib import Path
from uuid import uuid4

from pgvector import Vector

from rag_favorite import config as config_module
from rag_favorite import video_pipeline, video_worker
from rag_favorite.config import database_credentials, load_config
from rag_favorite.database import connect_database
from rag_favorite.pipeline_fence import llm_slot
from rag_favorite.video_config import VideoConfig
from rag_favorite.video_store import library_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    config = load_config(args.config)
    assert database_credentials(config)["name"].startswith("rag_memory_test_")
    root = args.root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    video = VideoConfig(
        root=root / "library",
        credentials_file=root / "unused",
        pipeline_enabled=True,
        ccr_concurrency=10,
    )
    video.root.mkdir()
    ids = [str(uuid4()) for _ in range(60)]
    with connect_database(config, register_pgvector=False) as c:
        for number, job_id in enumerate(ids):
            c.execute(
                "INSERT INTO public.rag_video_jobs(id,library_id,collection,state,stage,payload) VALUES(%s,%s,'general','queued','queued',%s::jsonb)",
                (
                    job_id,
                    library_id(video),
                    json.dumps({"title": f"synthetic memory fixture {number}"}),
                ),
            )
    original_process, original_config = video_worker.process, config_module.load_config
    active, peak = 0, 0
    lock, all_ten, stop_sampling = (
        threading.Lock(),
        threading.Event(),
        threading.Event(),
    )
    phases, samples = [], []
    cgroup = Path("/sys/fs/cgroup") / Path(
        "/proc/self/cgroup"
    ).read_text().strip().split(":", 2)[2].lstrip("/")

    def sample():
        while not stop_sampling.is_set():
            rss = int(
                next(
                    line.split()[1]
                    for line in Path("/proc/self/status").read_text().splitlines()
                    if line.startswith("VmRSS:")
                )
            )
            events = {
                key: int(value)
                for key, value in (
                    line.split()
                    for line in (cgroup / "memory.events").read_text().splitlines()
                )
            }
            samples.append(
                {
                    "at": time.time(),
                    "rss_kib": rss,
                    "memory_current": int((cgroup / "memory.current").read_text()),
                    "events": events,
                }
            )
            stop_sampling.wait(0.5)

    def process(current_config, current_video, job, *, phase):
        nonlocal active, peak
        path = video.root / "derived" / job["id"]
        path.mkdir(parents=True, exist_ok=True)
        if phase == "download":
            (path / "synthetic-source.bin").write_bytes(b"synthetic-fixture\n" * 1024)
        elif phase == "prepare":
            assert (path / "synthetic-source.bin").exists()
            video_worker.atomic_json(
                path / "prepared.json", {"fixture": True, "vector": [0.0] * 1024}
            )
        elif phase == "llm":
            assert (path / "prepared.json").exists()
            with llm_slot(current_video):
                with lock:
                    active += 1
                    peak = max(peak, active)
                    if active == 10:
                        all_ten.set()
                try:
                    assert all_ten.wait(30), "Ten consumers were not admitted"
                    # A bounded local working set per consumer, plus repeated real
                    # vector conversion/DB connections while all ten are active.
                    working = bytearray(32 * 1024**2)
                    for _ in range(20):
                        with connect_database(current_config) as c:
                            assert (
                                c.execute(
                                    "SELECT %b::vector <-> %t::vector",
                                    (Vector([1, 2, 3]), Vector([1, 2, 3])),
                                ).fetchone()[0]
                                == 0
                            )
                        time.sleep(0.5)
                    assert len(working) == 32 * 1024**2
                    video_worker.atomic_json(
                        path / "checkpoint.json", {"synthetic": True, "complete": True}
                    )
                finally:
                    with lock:
                        active -= 1
        elif phase == "publish":
            assert json.loads((path / "checkpoint.json").read_text())["complete"]
            with connect_database(current_config, register_pgvector=False) as c:
                c.execute(
                    "UPDATE public.rag_video_jobs SET state='complete',stage='complete' WHERE id=%s",
                    (job["id"],),
                )
        with lock:
            phases.append((job["id"], phase))

    video_worker.process = process
    config_module.load_config = lambda: config
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    started = time.monotonic()
    try:
        video_pipeline.run_pipeline(config, video, max_jobs=len(ids))
        with connect_database(config, register_pgvector=False) as c:
            completed = c.execute(
                "SELECT count(*) FROM public.rag_video_jobs WHERE id=ANY(%s::uuid[]) AND state='complete'",
                (ids,),
            ).fetchone()[0]
        assert completed == 60 and peak == 10 and len(phases) == 240
        assert all(
            not row["events"][key]
            for row in samples
            for key in ("high", "oom", "oom_kill")
        )
        video_worker.atomic_json(
            root / "result.json",
            {
                "passed": True,
                "jobs_completed": completed,
                "stage_completions": len(phases),
                "peak_concurrency": peak,
                "external_model_calls": 0,
                "real_media_uploaded": 0,
                "seconds": time.monotonic() - started,
                "max_rss_mib": max(row["rss_kib"] for row in samples) / 1024,
                "max_cgroup_mib": max(row["memory_current"] for row in samples)
                / 1024**2,
                "last_events": samples[-1]["events"],
                "samples": len(samples),
                "memory_limits": {
                    key: (cgroup / key).read_text().strip()
                    for key in ("memory.high", "memory.max", "memory.swap.max")
                },
                "scope": "real scheduler, leases, checkpoints, ten LLM slots and vector connections; synthetic phase bodies; no model/media inference",
            },
        )
        print((root / "result.json").read_text(), flush=True)
    finally:
        stop_sampling.set()
        sampler.join(timeout=3)
        video_worker.process, config_module.load_config = (
            original_process,
            original_config,
        )
        with connect_database(config, register_pgvector=False) as c:
            c.execute(
                "DELETE FROM public.rag_video_jobs WHERE id=ANY(%s::uuid[])", (ids,)
            )


if __name__ == "__main__":
    main()
