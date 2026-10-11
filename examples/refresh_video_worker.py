"""Load new worker code after the active job finishes, without interrupting model calls."""

from __future__ import annotations

import argparse
import json
import subprocess
import time

from rag_favorite.config import load_config
from rag_favorite.database import connect_database
from rag_favorite.queue_control import read_control, set_control
from rag_favorite.video_config import load_video_config
from rag_favorite.video_store import library_id
from rag_favorite.xhs_private import atomic_private_json, private_directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pause-stamp", required=True)
    args = parser.parse_args()
    config, video = load_config(), load_video_config()
    receipt = video.root / "following" / "worker-refresh.json"
    private_directory(receipt.parent)
    while True:
        with connect_database(config) as c:
            running = c.execute(
                "SELECT count(*) FROM public.rag_video_jobs WHERE library_id=%s AND state='running'",
                (library_id(video),),
            ).fetchone()[0]
        if not running:
            break
        atomic_private_json(
            receipt, {"state": "waiting_for_active_job", "running_jobs": running}
        )
        time.sleep(15)
    subprocess.run(
        ["systemctl", "--user", "restart", "rag-favorite-video-worker.service"],
        check=True,
    )
    current = read_control(video)
    restored = current.updated_at == args.pause_stamp
    if restored:
        set_control(video, paused=False)
    result = {
        "state": "complete",
        "queue_resumed": restored,
        "user_control_preserved": not restored,
    }
    atomic_private_json(receipt, result)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
