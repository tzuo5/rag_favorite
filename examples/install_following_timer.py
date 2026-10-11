"""Reversible owner-only following schedules, independent of the Codex window."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

from rag_favorite.config import load_config
from rag_favorite.migrations import MigrationRunner
from rag_favorite.video_config import load_video_config
from rag_favorite.video_store import ingestion_collection

REPO = Path(__file__).resolve().parents[1]
MARKER = "# Managed by rag_favorite/examples/install_following_timer.py\n"
NAMES = tuple(
    "rag-favorite-" + name
    for name in (
        "following.service",
        "following.timer",
        "following-continue.service",
        "following-continue.timer",
    )
)


def quote(value):
    return json.dumps(
        str(value).replace("%", "%%").replace("$", "$$"), ensure_ascii=False
    )


def units(config, video, collection, hours, timezone):
    ZoneInfo(timezone)
    if (
        not re.fullmatch(r"[A-Za-z0-9_/-]+", timezone)
        or not hours
        or any(not 0 <= h <= 23 for h in hours)
    ):
        raise ValueError("Invalid following schedule.")
    environment = "\n".join(
        "Environment=" + quote(k + "=" + str(v))
        for k, v in {
            "RAG_FAVORITE_CONFIG": config.source,
            "RAG_VIDEO_CONFIG": video.source,
            "PYTHONPATH": REPO / "src",
            "PYTHONUNBUFFERED": "1",
        }.items()
    )
    result = {}
    for continuation in (False, True):
        name = "rag-favorite-following" + ("-continue" if continuation else "")
        command = f"{quote(REPO / '.venv/bin/python')} -m rag_favorite.video_cli import-following --collection {quote(collection)} --seconds 1740"
        if continuation:
            command += " --continue-only"
        result[name + ".service"] = (
            MARKER
            + f"""[Unit]
Description=Discover followed Xiaohongshu authors into the existing ingestion queue
Requires=rag-favorite-runtime.service
After=rag-favorite-runtime.service
[Service]
Type=oneshot
WorkingDirectory={str(REPO).replace("%", "%%")}
{environment}
ExecStart={command}
SuccessExitStatus=2
TimeoutStartSec=30min
TimeoutStopSec=20
KillSignal=SIGINT
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
"""
        )
        calendar = (
            "*-*-* *:00,30:00 " + timezone
            if continuation
            else "*-*-* "
            + ",".join(f"{h:02d}" for h in sorted(set(hours)))
            + ":15:00 "
            + timezone
        )
        result[name + ".timer"] = (
            MARKER
            + f"""[Unit]
Description=Owner Xiaohongshu following {"continuation" if continuation else "daily synchronization"}
[Timer]
OnCalendar={calendar}
Persistent=true
AccuracySec=1min
Unit={name}.service
[Install]
WantedBy=timers.target
"""
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--collection", default="all")
    parser.add_argument("--hour", nargs="+", type=int, default=[7, 18])
    parser.add_argument("--timezone", default="America/Chicago")
    args = parser.parse_args()
    directory = Path.home() / ".config/systemd/user"

    def command(*values):
        subprocess.run(["systemctl", "--user", *values], check=True)

    def owned(path):
        if path.is_symlink():
            raise ValueError("Unit path is a symlink; installation refused.")
        return path.is_file() and path.read_text().startswith(MARKER)

    if args.uninstall:
        for name in NAMES:
            path = directory / name
            if owned(path):
                command("disable", "--now", name) if name.endswith(
                    ".timer"
                ) else command("stop", name)
                path.unlink()
        command("daemon-reload")
        print(json.dumps({"installed": False, "data_preserved": True}))
        return
    config = load_config(
        os.environ.get("RAG_FAVORITE_CONFIG", REPO / ".runtime/phase1/config.toml")
    )
    video = load_video_config(
        os.environ.get("RAG_VIDEO_CONFIG", REPO / ".runtime/videorag/video.toml")
    )
    collection = ingestion_collection(config, video, args.collection)
    if config.collection(collection).read_only:
        raise ValueError("Read-only collection.")
    generated = units(config, video, args.collection, args.hour, args.timezone)
    for name in NAMES:
        path = directory / name
        if (path.exists() or path.is_symlink()) and not owned(path):
            raise ValueError(
                "An unmanaged following unit exists; installation refused."
            )
    MigrationRunner(config).apply()
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".rag-following-units-", dir=directory
    ) as temporary:
        candidates = []
        for name, content in generated.items():
            path = Path(temporary) / name
            path.write_text(content)
            path.chmod(0o600)
            candidates.append(str(path))
        subprocess.run(["systemd-analyze", "--user", "verify", *candidates], check=True)
        for name in generated:
            os.replace(Path(temporary) / name, directory / name)
    command("daemon-reload")
    for name in NAMES:
        if name.endswith(".timer"):
            command("enable", "--now", name)
            command("restart", name)
    print(
        json.dumps(
            {
                "installed": True,
                "hours": args.hour,
                "timezone": args.timezone,
                "units": NAMES,
            }
        )
    )


if __name__ == "__main__":
    main()
