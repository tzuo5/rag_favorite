"""Real CPU/full-view/microbatch/GPU comparison with an outbound TCP guard."""

from __future__ import annotations

import ipaddress
import json
import resource
import socket
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from rag_favorite.imagebind_server import create_encoder_app
from rag_favorite.video_config import load_video_config
from rag_favorite.video_provider import local_encoder


def main():
    import decord
    import torch
    from imagebind import data

    video = load_video_config()
    root = video.root.parent / "test-fixtures" / "benchmark"
    scratch = video.root / "benchmark-work"
    scratch.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(dir=scratch)
    bounded = Path(temporary.name) / "bounded.mp4"
    subprocess.run(
        [
            video.ffmpeg,
            "-nostdin",
            "-y",
            "-i",
            str(root / "synthetic.mp4"),
            "-an",
            "-vf",
            "scale=512:512:force_original_aspect_ratio=decrease:force_divisible_by=2,fps=8",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(bounded),
        ],
        check=True,
        capture_output=True,
        timeout=180,
    )
    cpu_config = replace(video, device="cpu", dtype="float32")
    connect = socket.socket.connect
    blocked = []

    def loopback_only(sock, address):
        if (
            sock.family in (socket.AF_INET, socket.AF_INET6)
            and not ipaddress.ip_address(address[0]).is_loopback
        ):
            blocked.append(address[0])
            raise PermissionError("Benchmark outbound TCP is disabled.")
        return connect(sock, address)

    socket.socket.connect = loopback_only
    try:
        try:
            socket.create_connection(("192.0.2.1", 443), timeout=0.1)
        except PermissionError:
            pass
        gpu = local_encoder(video, "embed-video", {"path": str(bounded)})
        app = create_encoder_app(cpu_config)
        endpoint = next(r.endpoint for r in app.routes if r.path == "/embed-video")
        cpu = endpoint(
            {"path": str(bounded)},
            authorization="Bearer " + video.secret("RAG_ENCODER_TOKEN"),
        )
        with decord.bridge.use_torch():
            views = data.load_and_transform_video_data([str(bounded)], "cpu")
        started = time.monotonic()
        with torch.inference_mode():
            full = app.state.model({"vision": views})["vision"]
            full = torch.nn.functional.normalize(full.float(), dim=-1).flatten()
        full_seconds = time.monotonic() - started
        micro, gpu_vector = torch.tensor(cpu["vector"]), torch.tensor(gpu["vector"])
        result = {
            "fixture": "synthetic red frame with digit 2; not cooking corpus",
            "cpu_microbatch": {k: v for k, v in cpu.items() if k != "vector"},
            "cpu_full_batch_seconds": round(full_seconds, 3),
            "gpu_microbatch": {k: v for k, v in gpu.items() if k != "vector"},
            "views": list(views.shape),
            "cpu_full_vs_micro_max_abs": float((full - micro).abs().max()),
            "cpu_full_vs_micro_cosine": float(full @ micro),
            "cpu_fp32_vs_gpu_fp16_cosine": float(micro @ gpu_vector),
            "cpu_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * 1024,
            "blocked_non_loopback_tcp": blocked,
        }
        assert result["cpu_full_vs_micro_cosine"] > 0.99999
        assert result["cpu_fp32_vs_gpu_fp16_cosine"] > 0.995
        assert len(blocked) == 1
        Path("docs/implementation/imagebind-bounded-benchmark-results.json").write_text(
            json.dumps(result, indent=2)
        )
        print(json.dumps(result, indent=2))
    finally:
        socket.socket.connect = connect
        temporary.cleanup()


if __name__ == "__main__":
    main()
