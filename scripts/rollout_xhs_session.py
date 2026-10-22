"""Guarded local installation of the dedicated session; private rollback record."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

from rag_favorite.config import load_config
from rag_favorite.database import connect_database
from rag_favorite.migrations import MigrationRunner
from rag_favorite.queue_control import read_control, set_control
from rag_favorite.video_config import load_video_config
from rag_favorite.video_store import library_id
from rag_favorite.xhs_private import (
    atomic_private_json,
    browser_session_lock,
    private_directory,
    private_json,
)
from rag_favorite.xhs_session_rpc import SessionRPC

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / ".runtime/xhs-session-rollout-20261011"
UNIT = "rag-favorite-xhs-session.service"
TIMERS = [
    "rag-favorite-" + name + ".timer"
    for name in ("favorites", "following", "following-continue")
]
WORKER = "rag-favorite-video-worker.service"
WEB = "rag-favorite-web.service"


def systemctl(*args):
    return subprocess.check_output(["systemctl", "--user", *args], text=True).strip()


def active(unit):
    return systemctl("show", unit, "-p", "ActiveState", "--value") == "active"


def restore(before, video):
    if before["worker_active"]:
        systemctl("start", WORKER)
    set_control(video, paused=before["control"]["paused"])
    for unit in before["active_timers"]:
        systemctl("start", unit)


def source_hashes():
    files = json.loads((ROOT / "source-manifest.json").read_text())
    return {
        name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in files
    }


def rollback(config, video):
    """Retain jobs and the latest profile; refuse to overwrite subsequent edits."""
    expected = private_json(ROOT / "installed-source-hashes.json")
    if source_hashes() != expected:
        raise RuntimeError(
            "Source changed after rollout; inspect the diff before rollback."
        )
    record = ROOT / "rollback-runtime.json"
    if not record.exists():
        atomic_private_json(
            record,
            {
                "worker_active": active(WORKER),
                "web_active": active(WEB),
                "active_timers": [t for t in TIMERS if active(t)],
                "control": asdict(read_control(video)),
            },
        )
    previous = private_json(record)
    for timer in previous["active_timers"]:
        systemctl("stop", timer)
    set_control(video, paused=True)
    with connect_database(config, register_pgvector=False) as db:
        running = db.execute(
            "SELECT count(*) FROM rag_video_pipeline WHERE library_id=%s AND status='running'",
            (library_id(video),),
        ).fetchone()[0]
    if running or any(active(t.replace(".timer", ".service")) for t in TIMERS):
        raise RuntimeError(
            "Work is draining; rerun rollback once idle. Previous controls are saved."
        )
    if previous["worker_active"]:
        systemctl("stop", WORKER)
    systemctl("disable", "--now", UNIT)
    session = video.root.parent / "xhs-session"
    with browser_session_lock(session):
        if not (ROOT / "session-after").exists():
            shutil.copytree(
                session,
                ROOT / "session-after",
                symlinks=True,
                ignore=shutil.ignore_patterns("Singleton*", "session.sock"),
            )
    for name in expected:
        shutil.copy2(ROOT / "source-before" / name, REPO / name)
    # Additive schema stays installed; map any current hold to the old gate.
    with connect_database(config, register_pgvector=False) as db:
        db.execute(
            "UPDATE rag_video_pipeline_control SET xhs_login_required=xhs_login_required OR xhs_download_blocked WHERE library_id=%s",
            (library_id(video),),
        )
    if previous["web_active"]:
        systemctl("restart", WEB)
    restore(previous, video)
    print(
        json.dumps(
            {"rollback_complete": True, "jobs_and_current_profile_preserved": True}
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=("prepare", "install", "resume", "status", "rollback")
    )
    args = parser.parse_args()
    os.umask(0o077)
    private_directory(ROOT)
    config = load_config(REPO / ".runtime/phase1/config.toml")
    video = load_video_config(REPO / ".runtime/videorag/video.toml")
    before_path = ROOT / "before-runtime.json"
    if args.mode == "prepare":
        if before_path.exists():
            raise RuntimeError("Rollout already prepared; inspect the existing record.")
        with connect_database(config, register_pgvector=False) as db:
            gate = db.execute(
                "SELECT row_to_json(c) FROM public.rag_video_pipeline_control c WHERE library_id=%s",
                (library_id(video),),
            ).fetchone()
        before = {
            "worker_active": active(WORKER),
            "web_active": active(WEB),
            "active_timers": [unit for unit in TIMERS if active(unit)],
            "control": asdict(read_control(video)),
            "gate": gate[0] if gate else None,
        }
        atomic_private_json(before_path, before)
        for unit in before["active_timers"]:
            systemctl("stop", unit)
        print(
            json.dumps(
                {
                    "timers_paused": len(before["active_timers"]),
                    "original_queue_paused": before["control"]["paused"],
                }
            )
        )
        return
    before = private_json(before_path)
    if args.mode == "rollback":
        rollback(config, video)
        return
    if args.mode == "install":
        # Do not terminate a running discovery scan; it will checkpoint naturally.
        scans = [
            u.replace(".timer", ".service")
            for u in TIMERS
            if active(u.replace(".timer", ".service"))
        ]
        if scans:
            raise RuntimeError("Discovery still running; wait before installing.")
        set_control(video, paused=True)
        try:
            with connect_database(config, register_pgvector=False) as db:
                running = db.execute(
                    "SELECT count(*) FROM public.rag_video_pipeline WHERE library_id=%s AND status='running'",
                    (library_id(video),),
                ).fetchone()[0]
            if running:
                raise RuntimeError(
                    "Stages are draining; retry installation after they complete."
                )
            if before["worker_active"]:
                systemctl("stop", WORKER)
            session = video.root.parent / "xhs-session"
            if not (ROOT / "session-before").exists():
                with browser_session_lock(session, timeout=5):
                    shutil.copytree(
                        session,
                        ROOT / "session-before",
                        symlinks=True,
                        ignore=shutil.ignore_patterns("Singleton*", "session.sock"),
                    )
            changes = MigrationRunner(config).apply()
            template = (REPO / "src/rag_favorite/resources/systemd" / UNIT).read_text()
            values = {
                "REPO": REPO,
                "PYTHON": REPO / ".venv/bin/python",
                "CONFIG": REPO / ".runtime/phase1/config.toml",
                "VIDEO_CONFIG": REPO / ".runtime/videorag/video.toml",
            }
            for key, value in values.items():
                if key == "REPO":
                    template = template.replace("@REPO@", str(value).replace("%", "%%"))
                    continue
                template = template.replace(
                    "@" + key + "@",
                    '"' + str(value).replace("%", "%%").replace('"', '\\"') + '"',
                )
            units = Path.home() / ".config/systemd/user"
            temporary = units / (UNIT + ".new")
            temporary.write_text(template)
            temporary.chmod(0o600)
            temporary.replace(units / UNIT)
            subprocess.run(
                ["systemd-analyze", "--user", "verify", str(units / UNIT)], check=True
            )
            systemctl("daemon-reload")
            systemctl("enable", "--now", UNIT)
            for _ in range(60):
                try:
                    if SessionRPC(video).call("status")["service"] == "ready":
                        break
                except Exception:  # noqa: BLE001 - no private RPC details printed
                    time.sleep(1)
            else:
                raise RuntimeError("Session service did not become ready.")
            if before["web_active"]:
                systemctl("restart", WEB)
            print(
                json.dumps(
                    {
                        "installed": True,
                        "migrations": changes,
                        "worker_held_for_validation": True,
                    }
                )
            )
        except Exception:
            # Queue/timers remain deliberately held, with the old control saved.
            print(
                json.dumps(
                    {
                        "installation_incomplete": True,
                        "rollback_record": str(before_path),
                    }
                )
            )
            raise
        return
    if args.mode == "resume":
        state = SessionRPC(video).call("status")
        if state.get("service") != "ready":
            raise RuntimeError("Service is not ready; cannot resume.")
        restore(before, video)
        atomic_private_json(ROOT / "installed-source-hashes.json", source_hashes())
        atomic_private_json(ROOT / "resumed.json", {"at": time.time(), "state": state})
        print(
            json.dumps(
                {
                    "worker_restored": before["worker_active"],
                    "timers_restored": len(before["active_timers"]),
                    "queue_paused": read_control(video).paused,
                }
            )
        )
        return
    print(
        json.dumps(
            {
                "service": active(UNIT),
                "worker": active(WORKER),
                "queue_paused": read_control(video).paused,
                "state": SessionRPC(video).call("status"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
