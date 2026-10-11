"""Run in the isolated model environment. One loaded model, serialized inference."""

from __future__ import annotations

import gc
import hashlib
import hmac
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .video_config import load_video_config


def create_encoder_app(config=None):
    import av
    import torch
    from fastapi import FastAPI, Header, HTTPException
    from imagebind import data
    from imagebind.models import imagebind_model
    from imagebind.models.multimodal_preprocessors import SimpleTokenizer

    config = config or load_video_config()
    checkpoint = config.checkpoint
    if checkpoint is None or not checkpoint.is_file():
        raise RuntimeError("ImageBind checkpoint is missing.")
    digest = hashlib.sha256()
    with checkpoint.open("rb") as f:
        for block in iter(lambda: f.read(8_388_608), b""):
            digest.update(block)
    if digest.hexdigest() != config.checkpoint_sha256:
        raise RuntimeError("ImageBind checkpoint digest mismatch.")
    torch.set_num_threads(4)
    started = time.monotonic()
    model = imagebind_model.imagebind_huge(pretrained=False)
    state = torch.load(checkpoint, map_location="cpu", weights_only=True)
    model.load_state_dict(state, strict=True)
    del state
    # Verify the whole checkpoint before pruning unused modalities.
    for group in (
        model.modality_preprocessors,
        model.modality_trunks,
        model.modality_heads,
        model.modality_postprocessors,
    ):
        for key in list(group.keys()):
            if key not in {"vision", "text"}:
                del group[key]
    gc.collect()
    dtype = torch.float16 if config.dtype == "float16" else torch.float32
    model.eval().to(dtype=dtype).to(config.device)
    tokenizer = SimpleTokenizer(bpe_path=data.return_bpe_path())
    gpu_lock = threading.Lock()
    asr_lock = threading.Lock()
    app = FastAPI(title="rag-favorite local ImageBind", docs_url=None, redoc_url=None)
    app.state.model = model
    metrics = {
        "loads": 1,
        "load_seconds": round(time.monotonic() - started, 3),
        "device": config.device,
        "dtype": config.dtype,
    }

    def authorize(authorization):
        if not hmac.compare_digest(
            authorization or "", "Bearer " + config.secret("RAG_ENCODER_TOKEN")
        ):
            raise HTTPException(401, "Authentication required.")

    def owned_path(value):
        path = Path(value).resolve()
        if not path.is_relative_to(config.root.resolve()) or not path.is_file():
            raise HTTPException(400, "Asset is outside the owned video root.")
        return path

    def result(vector, started):
        vector = torch.nn.functional.normalize(vector.float(), dim=-1).detach().cpu()
        if not torch.isfinite(vector).all():
            raise HTTPException(503, "Encoder returned invalid values.")
        return {
            "vector": vector.flatten().tolist(),
            "space_id": config.space_id,
            "seconds": round(time.monotonic() - started, 3),
            **metrics,
            "cuda_peak_bytes": torch.cuda.max_memory_allocated()
            if config.device == "cuda"
            else 0,
        }

    asr_state = {"model": None}

    @app.get("/health")
    def health(authorization: str | None = Header(default=None)):
        authorize(authorization)
        return {"ok": True, "space_id": config.space_id, **metrics}

    @app.post("/probe")
    def probe(payload: dict, authorization: str | None = Header(default=None)):
        authorize(authorization)
        with av.open(str(owned_path(payload.get("path", "")))) as container:
            streams = container.streams.video
            if not streams or container.duration is None:
                raise HTTPException(400, "Video duration is unavailable.")
            return {
                "duration": container.duration / av.time_base,
                "width": streams[0].width,
                "height": streams[0].height,
                "has_audio": bool(container.streams.audio),
            }

    @app.post("/embed-text")
    def embed_text(payload: dict, authorization: str | None = Header(default=None)):
        authorize(authorization)
        text = payload.get("text", "")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(tokenizer.encode(text)) + 2 > 77
        ):
            raise HTTPException(400, "ImageBind query must fit 75 BPE content tokens.")
        with gpu_lock, torch.inference_mode():
            started = time.monotonic()
            inputs = data.load_and_transform_text([text], config.device)
            return result(model({"text": inputs})["text"], started)

    @app.post("/transcribe")
    def transcribe(payload: dict, authorization: str | None = Header(default=None)):
        authorize(authorization)
        path = owned_path(payload.get("path", ""))
        with av.open(str(path)) as audio:
            if audio.duration is None or audio.duration / av.time_base > 301:
                raise HTTPException(
                    400, "ASR input must be a timed slice of at most 300 seconds."
                )
        if config.asr_model is None or not config.asr_model.is_dir():
            raise HTTPException(503, "Local ASR model is not configured.")
        from faster_whisper import WhisperModel

        with asr_lock:
            if asr_state["model"] is None:
                asr_state["model"] = WhisperModel(
                    str(config.asr_model),
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=4,
                    local_files_only=True,
                )
            items, info = asr_state["model"].transcribe(
                str(path), beam_size=5, vad_filter=True, word_timestamps=True
            )
            segments = [
                {
                    "start": s.start,
                    "end": s.end,
                    "text": s.text.strip(),
                    "timing_precision": "segment",
                    "words": [
                        {"start": w.start, "end": w.end, "text": w.word}
                        for w in (s.words or [])
                    ],
                }
                for s in items
            ]
            if not segments:
                raise HTTPException(
                    422, "No speech detected; supply a transcript asset."
                )
            return {
                "segments": segments,
                "language": info.language,
                "language_probability": info.language_probability,
                "provider": f"local-faster-whisper-{config.asr_model.name.removeprefix('asr-')}-cpu-int8",
            }

    @app.post("/embed-video")
    def embed_video(payload: dict, authorization: str | None = Header(default=None)):
        authorize(authorization)
        path = owned_path(payload.get("path", ""))
        with gpu_lock, torch.inference_mode():
            started = time.monotonic()
            try:
                with av.open(str(path)) as probe:
                    if (
                        not probe.streams.video
                        or probe.duration is None
                        or probe.duration / av.time_base > 120
                    ):
                        raise HTTPException(
                            400,
                            "Encoder input must be a video segment of at most 120 seconds.",
                        )
                    stream = probe.streams.video[0]
                    bounded = (
                        max(stream.width, stream.height) <= 512
                        and stream.average_rate is not None
                        and float(stream.average_rate) <= 8
                    )
                temporary_root = config.root / "encoder-tmp"
                temporary_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tempfile.TemporaryDirectory(dir=temporary_root) as tmp:
                    prepared = path
                    if not bounded:
                        prepared = Path(tmp) / "bounded.mp4"
                        completed = subprocess.run(
                            [
                                config.ffmpeg,
                                "-nostdin",
                                "-y",
                                "-i",
                                str(path),
                                "-an",
                                "-vf",
                                "scale=512:512:force_original_aspect_ratio=decrease:force_divisible_by=2,fps=8",
                                "-c:v",
                                "libx264",
                                "-preset",
                                "fast",
                                "-pix_fmt",
                                "yuv420p",
                                str(prepared),
                            ],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                            timeout=180,
                            check=False,
                        )
                        if completed.returncode:
                            raise HTTPException(503, "Bounded video decoding failed.")
                    # Decord's bridge is thread-local. FastAPI can dispatch a
                    # later request to a new thread, so set it for every decode.
                    import decord

                    with decord.bridge.use_torch():
                        views = data.load_and_transform_video_data(
                            [str(prepared)], "cpu"
                        )
                if views.shape[1] != 15:
                    raise RuntimeError("Unexpected ImageBind view count.")
                vectors = [
                    model(
                        {
                            "vision": views[:, i : i + 1].to(
                                device=config.device, dtype=dtype
                            )
                        }
                    )["vision"].float()
                    for i in range(15)
                ]
                pooled = torch.stack(vectors).mean(dim=0)
                output = result(pooled, started)
                output["views"] = 15
                return output
            except torch.cuda.OutOfMemoryError:
                torch.cuda.empty_cache()
                raise HTTPException(
                    503, "GPU memory exhausted; retry with the explicit CPU profile."
                ) from None

    return app


def main():
    import uvicorn

    uvicorn.run(create_encoder_app(), host="127.0.0.1", port=9123, log_level="warning")


if __name__ == "__main__":
    main()
