"""Ensure the already-configured CCR gateway runs; never print its credentials."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path


def ready():
    try:
        with socket.create_connection(("127.0.0.1", 3456), timeout=2):
            return True
    except OSError:
        return False


def main():
    if ready():
        print("Existing local CCR gateway is listening.")
        return
    executable = shutil.which("ccr") or str(Path.home() / ".local/bin/ccr")
    if not Path(executable).is_file():
        raise SystemExit("Configured CCR executable is missing.")
    repo = Path(__file__).resolve().parents[1]
    log = repo / ".runtime/videorag/ccr-startup.log"
    log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "wb") as output:
        subprocess.run(
            [executable, "serve", "--daemon", "--no-open", "--gateway"],
            stdout=output,
            stderr=output,
            check=True,
            timeout=60,
        )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if ready():
            print("Configured local CCR gateway started.")
            return
        time.sleep(1)
    raise SystemExit("CCR gateway is not listening; inspect the private startup log.")


if __name__ == "__main__":
    main()
