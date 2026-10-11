"""Verify installed models; --prepare installs the isolated pinned model runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODEL_ROOT = REPO / ".runtime/video-encoder"
CHECKPOINT_SHA = "d6f6c22bedcc90708448d5d2fbb7b2db9c73f505dc89bd0b2e09b23af1b62157"


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(8_388_608), b""):
            value.update(block)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    python = MODEL_ROOT / "venv/bin/python"
    checkpoint = MODEL_ROOT / "models/imagebind_huge.pth"
    if args.prepare:
        uv = shutil.which("uv")
        if not uv:
            raise SystemExit(
                "Install uv in your user directory first; no sudo is required."
            )
        MODEL_ROOT.mkdir(parents=True, exist_ok=True)
        if not python.is_file():
            subprocess.run(
                [uv, "venv", "--python", "3.11", str(python.parent.parent)], check=True
            )
        subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                "--extra-index-url",
                "https://download.pytorch.org/whl/cu121",
                "-r",
                str(REPO / "examples/imagebind-requirements.lock.txt"),
            ],
            check=True,
        )
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        if not checkpoint.is_file():
            temporary = checkpoint.with_suffix(".partial")
            with (
                urllib.request.urlopen(
                    "https://dl.fbaipublicfiles.com/imagebind/imagebind_huge.pth",
                    timeout=60,
                ) as response,
                temporary.open("wb") as output,
            ):
                shutil.copyfileobj(response, output, 8_388_608)
            if digest(temporary) != CHECKPOINT_SHA:
                raise SystemExit(
                    "Checkpoint hash mismatch; partial file preserved for inspection."
                )
            temporary.replace(checkpoint)
        subprocess.run(
            [
                str(python),
                "-c",
                "from huggingface_hub import snapshot_download; import sys; snapshot_download('Systran/faster-whisper-medium', revision='08e178d48790749d25932bbc082711ddcfdfbc4f', local_dir=sys.argv[1])",
                str(MODEL_ROOT / "asr-medium"),
            ],
            check=True,
        )
    if not python.is_file() or not checkpoint.is_file():
        raise SystemExit("Model environment or checkpoint missing; run --prepare.")
    if digest(checkpoint) != CHECKPOINT_SHA:
        raise SystemExit("Checkpoint hash mismatch.")
    subprocess.run(
        [
            str(python),
            "-c",
            "import torch,imagebind, av, faster_whisper, imageio_ffmpeg; print({'torch':torch.__version__,'cuda_available':torch.cuda.is_available(),'ffmpeg':imageio_ffmpeg.get_ffmpeg_exe()})",
        ],
        check=True,
    )
    print(
        json.dumps(
            {
                "checkpoint_verified": True,
                "bytes": checkpoint.stat().st_size,
                "sha256": CHECKPOINT_SHA,
                "asr_present": (MODEL_ROOT / "asr-medium/model.bin").is_file(),
            }
        )
    )


if __name__ == "__main__":
    main()
