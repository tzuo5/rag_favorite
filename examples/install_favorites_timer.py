"""Reversible owner-only nightly favorites ingestion, independent of Codex UI."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

from rag_favorite.config import load_config
from rag_favorite.video_config import load_video_config
from rag_favorite.video_store import ingestion_collection
from rag_favorite.xhs_private import (
    atomic_private_json,
    private_directory,
    private_json,
)

REPO = Path(__file__).resolve().parents[1]
NAMES = ("rag-favorite-favorites.service", "rag-favorite-favorites.timer")
MARKER = "# Managed by rag_favorite/examples/install_favorites_timer.py\n"
UNITS = Path.home() / ".config/systemd/user"
BACKUP = REPO / ".runtime/videorag/favorites-timer-backup"


def quote(value):
    return json.dumps(
        str(value).replace("%", "%%").replace("$", "$$"), ensure_ascii=False
    )


def command(*args):
    return subprocess.run(["systemctl", "--user", *args], check=True)


def install(collection, hours, timezone):
    hours = sorted(set(hours))
    if (
        not re.fullmatch(r"[A-Za-z0-9_/-]+", timezone)
        or not hours
        or any(not 0 <= hour <= 23 for hour in hours)
    ):
        raise ValueError("Invalid nightly schedule.")
    ZoneInfo(timezone)
    config = load_config(
        os.environ.get("RAG_FAVORITE_CONFIG", REPO / ".runtime/phase1/config.toml")
    )
    video = load_video_config(
        os.environ.get("RAG_VIDEO_CONFIG", REPO / ".runtime/videorag/video.toml")
    )
    target_collection = ingestion_collection(config, video, collection)
    if config.collection(target_collection).read_only:
        raise ValueError("Selected collection is read-only.")
    private_directory(BACKUP)
    UNITS.mkdir(parents=True, exist_ok=True)
    receipt = BACKUP / "receipt.json"
    if not receipt.exists():
        previous = {}
        for name in NAMES:
            target = UNITS / name
            if target.is_symlink():
                raise ValueError(
                    "Existing timer unit is a symlink; preserve it and inspect manually."
                )
            if target.exists() and not target.read_text().startswith(MARKER):
                shutil.copyfile(target, BACKUP / name)
                (BACKUP / name).chmod(0o600)
                previous[name] = {
                    "saved": True,
                    "enabled": subprocess.run(
                        ["systemctl", "--user", "is-enabled", name],
                        capture_output=True,
                        text=True,
                        check=False,
                    ).stdout.strip()
                    == "enabled",
                }
            else:
                previous[name] = {"saved": False, "enabled": False}
        atomic_private_json(receipt, previous)
    environment = "\n".join(
        "Environment=" + quote(k + "=" + str(v))
        for k, v in {
            "RAG_FAVORITE_CONFIG": config.source,
            "RAG_VIDEO_CONFIG": video.source,
            "PYTHONPATH": REPO / "src",
            "PYTHONUNBUFFERED": "1",
        }.items()
    )
    service = (
        MARKER
        + f"""[Unit]
Description=Sync owner Xiaohongshu favorites into the RAG ingestion queue
Requires=rag-favorite-runtime.service
Wants=rag-favorite-video-worker.service
After=rag-favorite-runtime.service
[Service]
Type=oneshot
WorkingDirectory={str(REPO).replace("%", "%%")}
{environment}
ExecStart={quote(REPO / ".venv/bin/python")} -m rag_favorite.video_cli import-favorites --collection {quote(collection)} --restart
TimeoutStartSec=30min
TimeoutStopSec=20
KillSignal=SIGINT
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
"""
    )
    calendar_hours = ",".join(f"{hour:02d}" for hour in hours)
    timer = (
        MARKER
        + f"""[Unit]
Description=Scheduled owner Xiaohongshu favorites synchronization
[Timer]
OnCalendar=*-*-* {calendar_hours}:00:00 {timezone}
Persistent=true
AccuracySec=1min
Unit=rag-favorite-favorites.service
[Install]
WantedBy=timers.target
"""
    )
    # Validate temporary units before replacing any live unit configuration.
    with tempfile.TemporaryDirectory(dir=BACKUP, prefix="verify-") as directory:
        candidate = Path(directory)
        for name, text in zip(NAMES, (service, timer), strict=True):
            (candidate / name).write_text(text)
        subprocess.run(
            [
                "systemd-analyze",
                "--user",
                "verify",
                *(str(candidate / name) for name in NAMES),
            ],
            check=True,
        )
        for name in NAMES:
            target = UNITS / name
            if target.is_symlink():
                raise ValueError("Unsafe unit file path.")
            (candidate / name).chmod(0o600)
            os.replace(candidate / name, target)
    command("daemon-reload")
    command("enable", "--now", NAMES[1])
    command("restart", NAMES[1])
    print(
        json.dumps(
            {
                "installed": True,
                "collection": collection,
                "hours": hours,
                "timezone": timezone,
                "timer": NAMES[1],
            },
            ensure_ascii=False,
        )
    )


def uninstall():
    managed = [
        name
        for name in NAMES
        if (UNITS / name).is_file()
        and not (UNITS / name).is_symlink()
        and (UNITS / name).read_text().startswith(MARKER)
    ]
    previous = (
        private_json(BACKUP / "receipt.json")
        if (BACKUP / "receipt.json").exists()
        else {}
    )
    if NAMES[1] in managed:
        command("disable", "--now", NAMES[1])
    if NAMES[0] in managed:
        command("stop", NAMES[0])
    for name in managed:
        if previous.get(name, {}).get("saved"):
            shutil.copyfile(BACKUP / name, UNITS / name)
        else:
            (UNITS / name).unlink()
    command("daemon-reload")
    for name in managed:
        if previous.get(name, {}).get("enabled"):
            command("enable", name)
    print(
        "Nightly timer removed; session, database, knowledge and existing worker preserved."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--collection", default="all")
    parser.add_argument("--hour", type=int, nargs="+", default=[7, 18])
    parser.add_argument("--timezone", default="America/Chicago")
    args = parser.parse_args()
    if args.uninstall:
        uninstall()
    else:
        install(args.collection, args.hour, args.timezone)


if __name__ == "__main__":
    main()
