"""Unified command-line entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__
from .bootstrap import initialize_config
from .config import AppConfig, ConfigError, default_config, load_config, validate_config
from .embedding import EmbeddingError
from .mcp_support import (
    client_config,
    protocol_smoke,
    server_info,
    write_client_config,
)
from .migrations import MigrationError
from .rag import add_rag_commands
from .rag import run_cli as run_rag_cli
from .setup import SetupManager, SetupOptions, checks_ready, print_checks


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

    setup = commands.add_parser(
        "setup", help="Plan, apply or inspect first-run setup."
    )
    setup.add_argument(
        "setup_action", nargs="?", choices=("plan", "apply", "status"), default="apply"
    )
    setup.add_argument("--start-services", action="store_true")
    setup.add_argument("--pull-model", action="store_true")
    setup.add_argument("--skip-database", action="store_true")
    setup.add_argument("--render-systemd", action="store_true")
    setup.add_argument("--enable-services", action="store_true")
    setup.add_argument("--ingestion-dir", type=Path)
    setup.add_argument("--systemd-dir", type=Path)
    setup.add_argument("--openclaw-env", type=Path)
    setup.add_argument("--openclaw-media-dir", type=Path)
    setup.add_argument("--wait-seconds", type=int, default=60)
    setup.add_argument("--json", action="store_true")

    mcp = commands.add_parser("mcp", help="Configure and verify the MCP server.")
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_config = mcp_commands.add_parser(
        "config", help="Generate a standard mcpServers configuration fragment."
    )
    mcp_config.add_argument("--output", type=Path)
    mcp_config.add_argument("--force", action="store_true")
    mcp_commands.add_parser("inspect", help="Describe the packaged MCP server.")
    mcp_commands.add_parser("smoke", help="Run a real MCP stdio handshake.")

    add_rag_commands(commands, config)
    return parser


def _load_cli_config(raw: list[str]) -> AppConfig:
    selected, remaining = _preparse(raw)
    try:
        return load_config(selected)
    except ConfigError:
        if remaining[:2] != ["config", "init"] or "--force" not in remaining:
            raise
        defaults = default_config()
        source = (selected or defaults.source).expanduser().resolve()
        return replace(
            defaults,
            source=source,
            database=replace(
                defaults.database, credentials_file=source.parent / "secrets.env"
            ),
        )


def _execute(raw: list[str]) -> int:
    config = _load_cli_config(raw)
    parser = create_parser(config)
    arguments = parser.parse_args(raw)

    if arguments.command == "config":
        if arguments.config_command == "init":
            initialize_config(config.source, force=arguments.force)
            print(f"Configuration created: {config.source}")
            print(f"Credentials created: {config.source.parent / 'secrets.env'}")
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
    elif arguments.command == "setup":
        if arguments.enable_services and not arguments.render_systemd:
            raise ConfigError("--enable-services requires --render-systemd.")
        options = SetupOptions(
            start_services=arguments.start_services,
            migrate_database=not arguments.skip_database,
            pull_model=arguments.pull_model,
            render_systemd=arguments.render_systemd,
            enable_services=arguments.enable_services,
            ingestion_dir=arguments.ingestion_dir,
            systemd_dir=arguments.systemd_dir,
            openclaw_env=arguments.openclaw_env,
            openclaw_media_dir=arguments.openclaw_media_dir,
            wait_seconds=arguments.wait_seconds,
        )
        manager = SetupManager(config.source)
        if arguments.setup_action == "plan":
            checks = manager.plan(options)
        elif arguments.setup_action == "status":
            checks = manager.status()
        else:
            checks = manager.apply(options)
        print_checks(checks, as_json=arguments.json)
        if arguments.setup_action == "status" and not checks_ready(checks):
            return 1
    elif arguments.command == "mcp":
        if not config.source.is_file():
            raise ConfigError(
                f"Configuration is missing; run rag-favorite setup first: {config.source}"
            )
        if arguments.mcp_command == "config":
            payload = client_config(config)
            if arguments.output:
                destination = write_client_config(
                    arguments.output, payload, force=arguments.force
                )
                print(f"MCP configuration written: {destination}")
            else:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
        elif arguments.mcp_command == "inspect":
            print(json.dumps(server_info(config), ensure_ascii=False, indent=2))
        else:
            names = protocol_smoke(config)
            print("MCP stdio handshake passed: " + ", ".join(names))
    else:
        run_rag_cli(arguments, config)
    return 0


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        return _execute(raw)
    except KeyboardInterrupt:
        print("Operation cancelled.", file=sys.stderr)
        return 130
    except (ConfigError, EmbeddingError, MigrationError, RuntimeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
