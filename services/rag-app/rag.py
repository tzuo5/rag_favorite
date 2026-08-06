#!/usr/bin/env python3
"""Compatibility entrypoint for the historical services/rag-app path."""

from __future__ import annotations

import sys
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from rag_favorite.rag import *  # noqa: E402,F403
from rag_favorite.rag import create_parser, run_cli  # noqa: E402
from rag_favorite.config import load_config  # noqa: E402


def main() -> None:
    config = load_config()
    arguments = create_parser(config).parse_args()
    run_cli(arguments, config)


if __name__ == "__main__":
    main()
