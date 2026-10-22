"""Resolve the shared queue profiles without depending on a user interface."""

import os
from pathlib import Path

from .config import load_config
from .video_config import load_video_config


def profiles(config_path=None, video_path=None):
    root = Path(__file__).resolve().parents[2]
    default_config = root / ".runtime/phase1/config.toml"
    default_video = root / ".runtime/videorag/video.toml"
    selected_config = config_path or os.environ.get("RAG_FAVORITE_CONFIG")
    selected_video = video_path or os.environ.get("RAG_VIDEO_CONFIG")
    if selected_config is None and default_config.is_file():
        selected_config = default_config
    if selected_video is None and default_video.is_file():
        selected_video = default_video
    return load_config(selected_config), load_video_config(selected_video)
