"""Bridge ingestion to the installed rag-favorite product configuration."""
# ruff: noqa: I001

from __future__ import annotations

import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if SOURCE_ROOT.is_dir() and str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from rag_favorite.config import AppConfig, load_config


def product_config() -> AppConfig:
    return load_config()
