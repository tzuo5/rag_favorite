"""Install reversible user services for the prepared local runtime."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UNITS = Path.home() / ".config/systemd/user"
BACKUP = REPO / ".runtime/videorag/service-backups"
NAMES = (
    "rag-favorite-runtime.service",
    "rag-favorite-imagebind.service",
    "rag-favorite-video-worker.service",
    "rag-favorite-summary-worker.service",
)


def quote(value):
    return json.dumps(str(value).replace("%", "%%"), ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    if args.uninstall:
        managed = [
            name
            for name in NAMES
            if (UNITS / name).is_file()
            and (UNITS / name).read_text().startswith("# Managed by rag_favorite/")
        ]
        if managed:
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", *reversed(managed)],
                check=True,
            )
        for name in managed:
            target = UNITS / name
            if (BACKUP / name).exists():
                shutil.copyfile(BACKUP / name, target)
            else:
                target.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        print(
            "Managed user services removed; runtime data, models and credentials preserved."
        )
        return
    UNITS.mkdir(parents=True, exist_ok=True)
    BACKUP.mkdir(parents=True, exist_ok=True)
    text_profile = os.environ.get(
        "RAG_FAVORITE_CONFIG", REPO / ".runtime/phase1/config.toml"
    )
    video_profile = os.environ.get(
        "RAG_VIDEO_CONFIG", REPO / ".runtime/videorag/video.toml"
    )
    environment = "\n".join(
        "Environment=" + quote(k + "=" + str(v))
        for k, v in {
            "RAG_FAVORITE_CONFIG": text_profile,
            "RAG_VIDEO_CONFIG": video_profile,
            "PYTHONPATH": REPO / "src",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PYTHONUNBUFFERED": "1",
        }.items()
    )
    services = {
        "rag-favorite-runtime.service": f"""# Managed by rag_favorite/examples/install_video_services.py
[Unit]
Description=RAG local text model and isolated PostgreSQL
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash {quote(REPO / "examples/lmstudio_runtime.sh")} start
ExecStart={quote(REPO / ".venv/bin/python")} {quote(REPO / "examples/start_video_router.py")}
TimeoutStartSec=180
UMask=0077
[Install]
WantedBy=default.target
""",
    }
    for name, executable, module, dependency in [
        (
            "rag-favorite-imagebind.service",
            REPO / ".runtime/video-encoder/venv/bin/python",
            "rag_favorite.imagebind_server",
            "rag-favorite-runtime.service",
        ),
        (
            "rag-favorite-video-worker.service",
            REPO / ".venv/bin/python",
            "rag_favorite.video_worker",
            "rag-favorite-runtime.service rag-favorite-imagebind.service",
        ),
        (
            "rag-favorite-summary-worker.service",
            REPO / ".venv/bin/python",
            "rag_favorite.summary_worker",
            "rag-favorite-runtime.service",
        ),
    ]:
        services[
            name
        ] = f"""# Managed by rag_favorite/examples/install_video_services.py
[Unit]
Description=RAG {module}
Requires={dependency}
After={dependency}
[Service]
Type=simple
WorkingDirectory={str(REPO).replace("%", "%%")}
{environment}
ExecStart={quote(executable)} -m {module}
KillSignal={"SIGINT" if name == "rag-favorite-video-worker.service" else "SIGTERM"}
Restart=on-failure
RestartSec=20
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=yes
[Install]
WantedBy=default.target
"""
    for name, text in services.items():
        target = UNITS / name
        if (
            target.exists()
            and not (BACKUP / name).exists()
            and not target.read_text().startswith("# Managed by rag_favorite/")
        ):
            shutil.copyfile(target, BACKUP / name)
        target.write_text(text)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", *services], check=True)
    print(
        "User services installed and enabled. Start with bash examples/video_runtime.sh start."
    )


if __name__ == "__main__":
    main()
