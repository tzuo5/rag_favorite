"""Unified command-line entrypoint."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

from . import __version__
from .config import AppConfig, ConfigError, load_config, validate_config
from .embedding import EmbeddingError
from .paths import AppPaths
from .rag import add_rag_commands
from .rag import run_cli as run_rag_cli


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _config_template(paths: AppPaths) -> str:
    rows = [
        "# rag-favorite local configuration",
        "",
        "[database]",
        'host = "127.0.0.1"',
        "port = 5432",
        'name = "ragdb"',
        'user = "rag_admin"',
        'credentials_file = "secrets.env"',
        "",
        "[embedding]",
        'url = "http://127.0.0.1:11434/api/embed"',
        'model = "qwen3-embedding:0.6b"',
        "dimensions = 1024",
        "timeout_seconds = 120",
        "batch_size = 4",
    ]
    for collection in load_config().collections.values():
        rows.extend(
            [
                "",
                "[[collections]]",
                f"key = {_quote(collection.key)}",
                f"name = {_quote(collection.name)}",
                f"path = {_quote(str(paths.knowledge_dir / collection.key))}",
                f"template = {_quote(collection.template)}",
            ]
        )
    return "\n".join(rows) + "\n"


def initialize_config(path: Path, *, force: bool = False) -> None:
    selected = path.expanduser().resolve()
    if selected.exists() and not force:
        raise ConfigError(f"Configuration already exists: {selected}")
    selected.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    selected.write_text(_config_template(AppPaths.discover()), encoding="utf-8")
    selected.chmod(0o600)
    secrets_file = selected.parent / "secrets.env"
    if not secrets_file.exists():
        secrets_file.write_text(
            "RAG_DATABASE_PASSWORD=" + secrets.token_urlsafe(36) + "\n",
            encoding="utf-8",
        )
        secrets_file.chmod(0o600)
    config = load_config(selected)
    for collection in config.collections.values():
        collection.path.mkdir(parents=True, exist_ok=True)
    print(f"Configuration created: {selected}")
    print(f"Credentials created: {secrets_file}")


def _preparse(argv: list[str]) -> tuple[Path | None, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", type=Path)
    known, remaining = parser.parse_known_args(argv)
    return known.config, remaining


def create_parser(config: AppConfig) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rag-favorite",
        description="Manage a portable local-first RAG installation.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)

    config_parser = commands.add_parser("config", help="Manage product configuration.")
    config_commands = config_parser.add_subparsers(dest="config_command", required=True)
    init = config_commands.add_parser("init", help="Create a local configuration.")
    init.add_argument("--force", action="store_true")
    config_commands.add_parser("path", help="Print the active configuration path.")
    config_commands.add_parser("validate", help="Validate the active configuration.")

    collection = commands.add_parser(
        "collection", help="Inspect configured collections."
    )
    collection_commands = collection.add_subparsers(
        dest="collection_command", required=True
    )
    list_parser = collection_commands.add_parser("list")
    list_parser.add_argument("--json", action="store_true")

    add_rag_commands(commands, config)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    selected, _ = _preparse(raw)
    config = load_config(selected)
    parser = create_parser(config)
    arguments = parser.parse_args(raw)

    try:
        if arguments.command == "config":
            if arguments.config_command == "init":
                initialize_config(config.source, force=arguments.force)
            elif arguments.config_command == "path":
                print(config.source)
            else:
                validate_config(config)
                print(f"Configuration is valid: {config.source}")
        elif arguments.command == "collection":
            rows = [
                {
                    "key": item.key,
                    "name": item.name,
                    "path": str(item.path),
                    "template": item.template,
                    "read_only": item.read_only,
                }
                for item in config.collections.values()
            ]
            if arguments.json:
                print(json.dumps({"collections": rows}, ensure_ascii=False))
            else:
                for row in rows:
                    print(f"{row['key']}\t{row['name']}\t{row['path']}")
        else:
            run_rag_cli(arguments, config)
        return 0
    except KeyboardInterrupt:
        print("Operation cancelled.", file=sys.stderr)
        return 130
    except (ConfigError, EmbeddingError, RuntimeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
