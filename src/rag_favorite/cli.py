"""Unified command-line entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

from . import __version__
from .bootstrap import initialize_config
from .config import AppConfig, ConfigError, default_config, load_config, validate_config
from .embedding import EmbeddingError
from .ingestion import IngestionError, IngestionManager
from .mcp_support import (
    client_config,
    protocol_smoke,
    server_info,
    write_client_config,
)
from .migrations import MigrationError
from .openclaw import OpenClawConflictError, OpenClawError, OpenClawManager
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

    setup = commands.add_parser("setup", help="Plan, apply or inspect first-run setup.")
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

    openclaw = commands.add_parser(
        "openclaw", help="Manage the optional OpenClaw MCP registration."
    )
    openclaw_commands = openclaw.add_subparsers(dest="openclaw_command", required=True)
    openclaw_common = argparse.ArgumentParser(add_help=False)
    openclaw_common.add_argument("--openclaw-executable", type=Path)
    openclaw_common.add_argument("--openclaw-config", type=Path)
    openclaw_common.add_argument("--json", action="store_true")
    openclaw_commands.add_parser(
        "detect", parents=[openclaw_common], help="Detect OpenClaw capabilities."
    )
    openclaw_plan = openclaw_commands.add_parser(
        "plan", parents=[openclaw_common], help="Preview registration changes."
    )
    openclaw_plan.add_argument("--replace", action="store_true")
    openclaw_install = openclaw_commands.add_parser(
        "install", parents=[openclaw_common], help="Register the MCP server."
    )
    openclaw_install.add_argument("--replace", action="store_true")
    openclaw_install.add_argument("--no-probe", action="store_true")
    openclaw_status = openclaw_commands.add_parser(
        "status", parents=[openclaw_common], help="Inspect registration status."
    )
    openclaw_status.add_argument("--probe", action="store_true")
    openclaw_uninstall = openclaw_commands.add_parser(
        "uninstall", parents=[openclaw_common], help="Remove the MCP registration."
    )
    openclaw_uninstall.add_argument("--force", action="store_true")
    openclaw_commands.add_parser(
        "backups", parents=[openclaw_common], help="List managed config backups."
    )
    openclaw_restore = openclaw_commands.add_parser(
        "restore", parents=[openclaw_common], help="Restore a managed config backup."
    )
    openclaw_restore.add_argument("metadata", type=Path)
    openclaw_restore.add_argument("--force", action="store_true")

    ingestion = commands.add_parser(
        "ingestion", help="Manage optional media ingestion and Telegram integration."
    )
    ingestion_commands = ingestion.add_subparsers(
        dest="ingestion_command", required=True
    )
    ingestion_common = argparse.ArgumentParser(add_help=False)
    ingestion_common.add_argument("--project-dir", type=Path)
    ingestion_common.add_argument("--json", action="store_true")
    ingestion_openclaw = argparse.ArgumentParser(add_help=False)
    ingestion_openclaw.add_argument("--with-openclaw", action="store_true")
    ingestion_openclaw.add_argument("--openclaw-executable", type=Path)
    ingestion_openclaw.add_argument("--openclaw-config", type=Path)
    ingestion_commands.add_parser(
        "detect", parents=[ingestion_common], help="Detect ingestion prerequisites."
    )
    ingestion_plan = ingestion_commands.add_parser(
        "plan",
        parents=[ingestion_common, ingestion_openclaw],
        help="Preview ingestion installation changes.",
    )
    ingestion_install = ingestion_commands.add_parser(
        "install",
        parents=[ingestion_common, ingestion_openclaw],
        help="Apply the reviewed ingestion installation.",
    )
    for selected in (ingestion_plan, ingestion_install):
        selected.add_argument("--install-dependencies", action="store_true")
        selected.add_argument("--render-systemd", action="store_true")
        selected.add_argument("--enable-services", action="store_true")
    ingestion_commands.add_parser(
        "status",
        parents=[ingestion_common, ingestion_openclaw],
        help="Inspect ingestion and optional plugin readiness.",
    )
    ingestion_uninstall = ingestion_commands.add_parser(
        "uninstall",
        parents=[ingestion_common, ingestion_openclaw],
        help="Disable integrations while preserving data and credentials.",
    )
    ingestion_uninstall.add_argument("--disable-services", action="store_true")
    ingestion_uninstall.add_argument("--force", action="store_true")

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
    elif arguments.command == "openclaw":
        manager = OpenClawManager(
            config,
            executable=arguments.openclaw_executable,
            openclaw_config=arguments.openclaw_config,
        )
        if arguments.openclaw_command == "detect":
            detection = manager.detect()
            result = {**asdict(detection), "ready": detection.ready}
        elif arguments.openclaw_command == "plan":
            result = manager.plan(replace=arguments.replace)
        elif arguments.openclaw_command == "install":
            result = manager.install(
                replace=arguments.replace,
                probe=not arguments.no_probe,
            )
        elif arguments.openclaw_command == "status":
            result = manager.status(probe=arguments.probe)
        elif arguments.openclaw_command == "uninstall":
            result = manager.uninstall(force=arguments.force)
        elif arguments.openclaw_command == "backups":
            result = {"backups": list(manager.list_backups())}
        else:
            result = manager.restore(arguments.metadata, force=arguments.force)
        if arguments.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            for key, value in result.items():
                print(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        if (
            arguments.openclaw_command == "plan" and result.get("action") == "conflict"
        ) or (arguments.openclaw_command == "status" and not result.get("ready")):
            return 1
    elif arguments.command == "ingestion":
        manager = IngestionManager(config, project_dir=arguments.project_dir)
        plugin_manager = None
        if getattr(arguments, "with_openclaw", False):
            plugin_manager = manager.openclaw_plugin_manager(
                executable=arguments.openclaw_executable,
                openclaw_config=arguments.openclaw_config,
            )
        if arguments.ingestion_command == "detect":
            detection = manager.detect()
            result = {**asdict(detection), "ready": detection.ready}
        elif arguments.ingestion_command == "plan":
            result = manager.plan(
                install_dependencies=arguments.install_dependencies,
                render_systemd=arguments.render_systemd,
                enable_services=arguments.enable_services,
                register_openclaw=arguments.with_openclaw,
                plugin_manager=plugin_manager,
            )
        elif arguments.ingestion_command == "install":
            result = manager.install(
                install_dependencies=arguments.install_dependencies,
                render_systemd=arguments.render_systemd,
                enable_services=arguments.enable_services,
                register_openclaw=arguments.with_openclaw,
                plugin_manager=plugin_manager,
            )
        elif arguments.ingestion_command == "status":
            result = manager.status(
                inspect_openclaw=arguments.with_openclaw,
                plugin_manager=plugin_manager,
            )
        else:
            result = manager.uninstall(
                unregister_openclaw=arguments.with_openclaw,
                disable_services=arguments.disable_services,
                force=arguments.force,
                plugin_manager=plugin_manager,
            )
        if arguments.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            for key, value in result.items():
                print(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        if arguments.ingestion_command == "status" and not result.get("ready"):
            return 1
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
    except (
        ConfigError,
        EmbeddingError,
        IngestionError,
        MigrationError,
        OpenClawConflictError,
        OpenClawError,
        RuntimeError,
        OSError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
