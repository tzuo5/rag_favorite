"""Separate video profile: core text installations remain dependency-light."""

from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .config import ConfigError


def private_environment(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if path.stat().st_mode & 0o077:
        raise ConfigError("Video credentials must have mode 0600.")
    values = {}
    for line in path.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


@dataclass(frozen=True)
class VideoConfig:
    root: Path
    credentials_file: Path
    encoder_url: str = "http://127.0.0.1:9123"
    router_url: str = "http://127.0.0.1:3456/v1/responses"
    model: str = "Codex API/gpt-6.1-sol"
    visual_enabled: bool = False
    segment_seconds: int = 30
    long_video_segment_seconds: int = 60
    ffmpeg: str = "ffmpeg"
    device: str = "cuda"
    dtype: str = "float32"
    asr_model: Path | None = None
    checkpoint: Path | None = None
    checkpoint_sha256: str = ""
    max_asset_bytes: int = 2_000_000_000
    failed_media_ttl_hours: int = 72
    visual_budget_seconds: int = 0  # Zero disables the cumulative time budget.
    silent_frame_interval_seconds: float = 0.5
    speech_frame_interval_seconds: float = 1.0
    visual_max_duration_seconds: float = 600
    visual_frames_per_request: int = 8
    visual_request_budget: int = 100
    visual_image_budget: int = 100
    source: Path | None = None
    default_collection: str = "cooking"
    pipeline_enabled: bool = False
    pipeline_prefetch: int = 2
    pipeline_temp_bytes: int = 10 * 1024**3

    def secret(self, key: str) -> str:
        value = os.environ.get(key) or private_environment(self.credentials_file).get(
            key
        )
        if not value:
            raise ConfigError(f"Missing video credential: {key}")
        return value

    @property
    def space_id(self) -> str:
        payload = (
            "imagebind:53680b02d7e37b19b124fa37bae4b6c98c38f5be:"
            + self.checkpoint_sha256
            + ":vision-text:1024:decode-maxedge512-fps8:clip2-count5-crops3-224:mean-l2:"
            + self.dtype
            + ":v1"
        )
        return hashlib.sha256(payload.encode()).hexdigest()


def load_video_config(path: Path | str | None = None) -> VideoConfig:
    selected = (
        Path(path or os.environ.get("RAG_VIDEO_CONFIG", "video.toml"))
        .expanduser()
        .resolve()
    )
    if not selected.is_file():
        raise ConfigError("Video profile is missing; set RAG_VIDEO_CONFIG.")
    raw = tomllib.loads(selected.read_text())

    def absolute(value: str) -> Path:
        p = Path(value).expanduser()
        return p.resolve() if p.is_absolute() else (selected.parent / p).resolve()

    config = VideoConfig(
        source=selected,
        pipeline_enabled=raw.get("pipeline_enabled", True),
        pipeline_prefetch=int(raw.get("pipeline_prefetch", 2)),
        pipeline_temp_bytes=int(raw.get("pipeline_temp_bytes", 10 * 1024**3)),
        default_collection=str(raw.get("default_collection", "cooking")),
        root=absolute(raw.get("root", "video-data")),
        credentials_file=absolute(raw.get("credentials_file", "credentials.env")),
        encoder_url=raw.get("encoder_url", "http://127.0.0.1:9123"),
        router_url=raw.get("router_url", "http://127.0.0.1:3456/v1/responses"),
        model=raw.get("model", "Codex API/gpt-6.1-sol"),
        visual_enabled=raw.get("visual_enabled", False),
        segment_seconds=int(raw.get("segment_seconds", 30)),
        long_video_segment_seconds=int(raw.get("long_video_segment_seconds", 60)),
        ffmpeg=raw.get("ffmpeg", "ffmpeg"),
        device=raw.get("device", "cuda"),
        dtype=raw.get("dtype", "float32"),
        asr_model=absolute(raw["asr_model"]) if raw.get("asr_model") else None,
        checkpoint=absolute(raw["checkpoint"]) if raw.get("checkpoint") else None,
        checkpoint_sha256=raw.get("checkpoint_sha256", ""),
        max_asset_bytes=int(raw.get("max_asset_bytes", 2_000_000_000)),
        failed_media_ttl_hours=int(raw.get("failed_media_ttl_hours", 72)),
        visual_budget_seconds=int(raw.get("visual_budget_seconds", 0)),
        silent_frame_interval_seconds=float(
            raw.get("silent_frame_interval_seconds", 0.5)
        ),
        speech_frame_interval_seconds=float(
            raw.get("speech_frame_interval_seconds", 1.0)
        ),
        visual_max_duration_seconds=float(raw.get("visual_max_duration_seconds", 600)),
        visual_frames_per_request=int(raw.get("visual_frames_per_request", 8)),
    )
    for url in (config.encoder_url, config.router_url):
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ConfigError("This video profile requires loopback HTTP services.")
    if (
        not 2 <= config.segment_seconds <= 120
        or type(config.pipeline_enabled) is not bool
        or not 1 <= config.pipeline_prefetch <= 5
        or config.pipeline_temp_bytes < config.max_asset_bytes
        or not config.segment_seconds <= config.long_video_segment_seconds <= 120
        or config.device not in {"cpu", "cuda"}
        or config.dtype not in {"float32", "float16"}
        or (config.device == "cpu" and config.dtype != "float32")
        or config.failed_media_ttl_hours < 1
        or not 0.1 <= config.silent_frame_interval_seconds <= 10
        or not 0.1 <= config.speech_frame_interval_seconds <= 10
        or not 1 <= config.visual_max_duration_seconds <= 600
        or not 1 <= config.visual_frames_per_request <= 8
        or (
            config.visual_budget_seconds != 0
            and not 60 <= config.visual_budget_seconds <= 3600
        )
    ):
        raise ConfigError("Invalid video segment length or device.")
    return config
