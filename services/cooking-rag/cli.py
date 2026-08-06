from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from embedding import OllamaEmbeddingClient
from repository import RecipeIndexer, RecipeRetriever
from product_config import CONFIG

DEFAULT_VAULT = CONFIG.collection("cooking").path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Isolated cooking RAG administration")
    parser.add_argument(
        "--env-file",
        type=Path,
        help="database env file; never pass credentials as command arguments",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index")
    index_parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    index_parser.add_argument("--prune", action="store_true")

    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=5)

    subparsers.add_parser("status")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.env_file:
        os.environ["COOKING_RAG_ENV_FILE"] = str(args.env_file.resolve())

    client = OllamaEmbeddingClient()
    if args.command == "index":
        result = RecipeIndexer(args.vault, client).index(prune=args.prune)
    else:
        retriever = RecipeRetriever(client)
        result = (
            retriever.search(args.query, args.limit)
            if args.command == "search"
            else retriever.status()
        )

    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
