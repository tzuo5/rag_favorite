"""Idempotent first-run setup orchestration."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg

from . import __version__
from .bootstrap import ensure_directories, initialize_config
from .config import AppConfig, ConfigError, database_credentials, load_config
from .database import connect_database
from .migrations import MIGRATIONS, MigrationRunner

SYSTEMD_RESOURCE_PACKAGE = "rag_favorite.resources.systemd"
COMPOSE_RESOURCE_PACKAGE = "rag_favorite.resources"
SYSTEMD_UNITS = (
    "bilibili-session-manager.service",
    "video-author-discovery-worker.service",
    "video-batch-notification-worker.service",
    "video-ingestion-cleanup.service",
    "video-ingestion-cleanup.timer",
    "video-ingestion-worker.service",
    "xhs-session-check.timer",
    "xhs-session-manager.service",
)
DEFAULT_ENABLED_UNITS = (
    "video-author-discovery-worker.service",
    "video-batch-notification-worker.service",
    "video-ingestion-cleanup.timer",
    "video-ingestion-worker.service",
)


@dataclass(frozen=True, slots=True)
class SetupCheck:
    name: str
    status: str
    detail: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class SetupOptions:
    start_services: bool = False
    migrate_database: bool = True
    pull_model: bool = False
    render_systemd: bool = False
    enable_services: bool = False
    ingestion_dir: Path | None = None
    systemd_dir: Path | None = None
    openclaw_env: Path | None = None
    openclaw_media_dir: Path | None = None
    wait_seconds: int = 60


def _safe_path(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if any(character.isspace() or ord(character) < 32 for character in str(resolved)):
        raise ConfigError(f"{label} cannot contain whitespace or control characters.")
    return resolved


def _command_runner(
    command: Sequence[str], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, check=False, **kwargs)


class SetupManager:
    def __init__(
        self,
        config_path: Path,
        *,
        command_runner: Callable[
            ..., subprocess.CompletedProcess[str]
        ] = _command_runner,
        migration_runner_factory: Callable[
            [AppConfig], MigrationRunner
        ] = MigrationRunner,
        database_connector: Callable[..., Any] = connect_database,
    ) -> None:
        self.config_path = config_path.expanduser().resolve()
        self._run = command_runner
        self._migration_runner_factory = migration_runner_factory
        self._database_connector = database_connector

    def plan(self, options: SetupOptions) -> tuple[SetupCheck, ...]:
        checks = [
            SetupCheck(
                "configuration",
                "preserve" if self.config_path.exists() else "create",
                str(self.config_path),
            ),
            SetupCheck(
                "directories", "ensure", "XDG runtime and collection directories"
            ),
        ]
        if options.start_services:
            checks.append(
                SetupCheck(
                    "local-services",
                    "start",
                    "PostgreSQL/pgvector and Ollama via Docker Compose",
                )
            )
        else:
            checks.append(
                SetupCheck(
                    "local-services",
                    "external",
                    "use existing loopback PostgreSQL and Ollama",
                    required=False,
                )
            )
        checks.append(
            SetupCheck(
                "database-migrations",
                "apply" if options.migrate_database else "skip",
                ", ".join(MIGRATIONS),
                required=options.migrate_database,
            )
        )
        if options.render_systemd:
            checks.append(
                SetupCheck(
                    "user-systemd",
                    "render-and-enable" if options.enable_services else "render",
                    str(self._systemd_dir(options)),
                )
            )
        return tuple(checks)

    def apply(self, options: SetupOptions) -> tuple[SetupCheck, ...]:
        if options.pull_model and not options.start_services:
            raise ConfigError("--pull-model requires --start-services.")
        if options.enable_services and not options.render_systemd:
            raise ConfigError("--enable-services requires --render-systemd.")
        created = not self.config_path.exists()
        if created:
            config = initialize_config(self.config_path)
        else:
            config = load_config(self.config_path)
            ensure_directories(config)

        results: list[SetupCheck] = [
            SetupCheck(
                "configuration",
                "created" if created else "preserved",
                str(config.source),
            ),
            SetupCheck(
                "directories", "ready", "XDG runtime and collection directories"
            ),
        ]
        if options.start_services:
            compose_file = self._start_services(config)
            results.append(SetupCheck("local-services", "started", str(compose_file)))
            self._wait_for_database(config, options.wait_seconds)
        if options.pull_model:
            self._compose(
                config, "exec", "-T", "ollama", "ollama", "pull", config.embedding.model
            )
            results.append(
                SetupCheck("embedding-model", "pulled", config.embedding.model)
            )
        applied: tuple[str, ...] = ()
        if options.migrate_database:
            applied = self._migration_runner_factory(config).apply()
            results.append(
                SetupCheck(
                    "database-migrations",
                    "applied" if applied else "current",
                    ", ".join(applied or MIGRATIONS),
                )
            )
        if options.render_systemd:
            unit_dir = self._render_systemd(config, options)
            status = "rendered"
            if options.enable_services:
                self._systemctl("daemon-reload")
                self._systemctl("enable", "--now", *DEFAULT_ENABLED_UNITS)
                status = "enabled"
            results.append(SetupCheck("user-systemd", status, str(unit_dir)))
        self._write_state(config, results)
        return tuple(results)

    def status(self) -> tuple[SetupCheck, ...]:
        if not self.config_path.is_file():
            return (
                SetupCheck(
                    "configuration",
                    "missing",
                    f"Run rag-favorite --config {self.config_path} setup apply",
                ),
            )
        try:
            config = load_config(self.config_path)
        except ConfigError as exc:
            return (SetupCheck("configuration", "error", str(exc)),)

        checks: list[SetupCheck] = [
            SetupCheck("configuration", "ready", str(config.source)),
            SetupCheck(
                "directories",
                "ready"
                if all(item.path.is_dir() for item in config.collections.values())
                else "missing",
                "configured collection roots",
            ),
        ]
        try:
            records = self._migration_runner_factory(config).status()
            versions = {record.version for record in records}
            missing = [version for version in MIGRATIONS if version not in versions]
            checks.append(
                SetupCheck(
                    "database",
                    "ready" if not missing else "migration-required",
                    ", ".join(missing)
                    if missing
                    else f"{len(records)} migrations applied",
                )
            )
        except (OSError, RuntimeError, psycopg.Error) as exc:
            checks.append(SetupCheck("database", "unavailable", str(exc)))
        checks.append(self._embedding_status(config))
        checks.append(
            SetupCheck(
                "ffmpeg",
                "ready" if shutil.which("ffmpeg") else "missing",
                shutil.which("ffmpeg") or "required only for media ingestion",
                required=False,
            )
        )
        return tuple(checks)

    def _runtime_dir(self, config: AppConfig) -> Path:
        return config.paths.state_dir / "runtime"

    def _compose_file(self, config: AppConfig) -> Path:
        return self._runtime_dir(config) / "compose.yaml"

    def _start_services(self, config: AppConfig) -> Path:
        if not shutil.which("docker"):
            raise ConfigError("Docker is required for --start-services.")
        runtime_dir = self._runtime_dir(config)
        runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        compose = (
            resources.files(COMPOSE_RESOURCE_PACKAGE)
            .joinpath("compose.yaml")
            .read_text(encoding="utf-8")
        )
        compose_file = self._compose_file(config)
        compose_file.write_text(compose, encoding="utf-8")
        compose_file.chmod(0o600)
        self._compose(config, "up", "-d", "postgres", "ollama")
        return compose_file

    def _compose(self, config: AppConfig, *arguments: str) -> None:
        credentials = database_credentials(config)
        environment = os.environ.copy()
        environment.update(
            {
                "RAG_DATABASE_NAME": credentials["name"],
                "RAG_DATABASE_USER": credentials["user"],
                "RAG_DATABASE_PASSWORD": credentials["password"],
                "RAG_DATABASE_PORT": str(config.database.port),
                "RAG_OLLAMA_PORT": str(urlparse(config.embedding.url).port or 11434),
            }
        )
        command = [
            "docker",
            "compose",
            "--project-name",
            "rag-favorite",
            "--file",
            str(self._compose_file(config)),
            *arguments,
        ]
        result = self._run(command, capture_output=True, env=environment)
        if result.returncode:
            detail = (result.stderr or result.stdout or "Docker Compose failed").strip()
            raise RuntimeError(detail)

    def _wait_for_database(self, config: AppConfig, wait_seconds: int) -> None:
        deadline = time.monotonic() + max(1, wait_seconds)
        last_error = "not ready"
        while time.monotonic() < deadline:
            try:
                connection = self._database_connector(
                    config,
                    register_pgvector=False,
                    application_name="rag-favorite-setup-wait",
                )
                connection.close()
                return
            except (OSError, RuntimeError, psycopg.Error) as exc:
                last_error = str(exc)
                time.sleep(1)
        raise RuntimeError(f"PostgreSQL did not become ready: {last_error}")

    def _systemd_dir(self, options: SetupOptions) -> Path:
        if options.systemd_dir:
            return options.systemd_dir.expanduser().resolve()
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return config_home / "systemd" / "user"

    def _render_systemd(self, config: AppConfig, options: SetupOptions) -> Path:
        if options.ingestion_dir is None:
            raise ConfigError("--ingestion-dir is required with --render-systemd.")
        ingestion_dir = _safe_path(options.ingestion_dir, "Ingestion directory")
        if not (ingestion_dir / "backend").is_dir():
            raise ConfigError(f"Invalid ingestion directory: {ingestion_dir}")
        if not (ingestion_dir / ".env").is_file():
            raise ConfigError(
                f"Missing ingestion environment file: {ingestion_dir / '.env'}"
            )
        if (
            options.enable_services
            and not (ingestion_dir / ".venv/bin/python").is_file()
        ):
            raise ConfigError(
                f"Missing ingestion virtual environment: {ingestion_dir / '.venv'}"
            )
        openclaw_env = _safe_path(
            options.openclaw_env or config.paths.config_dir / "openclaw.env",
            "OpenClaw environment file",
        )
        if not openclaw_env.exists():
            openclaw_env.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            openclaw_env.write_text(
                "# Optional OpenClaw environment\n", encoding="utf-8"
            )
            openclaw_env.chmod(0o600)
        media_dir = _safe_path(
            options.openclaw_media_dir or config.paths.data_dir / "openclaw-media",
            "OpenClaw media directory",
        )
        product_data_dir = _safe_path(config.paths.data_dir, "Product data directory")
        (media_dir / "inbound").mkdir(parents=True, exist_ok=True)
        for directory in (
            product_data_dir / "sessions" / "bilibili",
            product_data_dir / "sessions" / "xiaohongshu",
            ingestion_dir / "secrets",
            ingestion_dir / "staging",
            ingestion_dir / "temp",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        unit_dir = self._systemd_dir(options)
        unit_dir.mkdir(parents=True, exist_ok=True)
        writable_collections = [
            _safe_path(collection.path, f"Collection {collection.key} directory")
            for collection in config.collections.values()
            if not collection.read_only
        ]
        if not writable_collections:
            raise ConfigError("Systemd ingestion requires a writable collection.")
        replacements = {
            "@INGESTION_DIR@": str(ingestion_dir),
            "@OPENCLAW_ENV@": str(openclaw_env),
            "@PRODUCT_DATA_DIR@": str(product_data_dir),
            "@KNOWLEDGE_DIR@": " ".join(str(path) for path in writable_collections),
            "@OPENCLAW_MEDIA_DIR@": str(media_dir),
        }
        root = resources.files(SYSTEMD_RESOURCE_PACKAGE)
        for name in SYSTEMD_UNITS:
            content = root.joinpath(name).read_text(encoding="utf-8")
            for token, value in replacements.items():
                content = content.replace(token, value)
            if "@" in content:
                raise RuntimeError(f"Unresolved template token in {name}.")
            destination = unit_dir / name
            destination.write_text(content, encoding="utf-8")
            destination.chmod(0o600)
        return unit_dir

    def _systemctl(self, *arguments: str) -> None:
        if not shutil.which("systemctl"):
            raise ConfigError("systemctl is required for --enable-services.")
        result = self._run(["systemctl", "--user", *arguments], capture_output=True)
        if result.returncode:
            detail = (result.stderr or result.stdout or "systemctl failed").strip()
            raise RuntimeError(detail)

    def _embedding_status(self, config: AppConfig) -> SetupCheck:
        parsed = urlparse(config.embedding.url)
        tags_url = f"{parsed.scheme}://{parsed.netloc}/api/tags"
        try:
            with urllib.request.urlopen(tags_url, timeout=2) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                payload = json.load(response)
            installed = {
                str(model.get(field))
                for model in payload.get("models", [])
                if isinstance(model, dict)
                for field in ("name", "model")
                if model.get(field)
            }
            if config.embedding.model not in installed:
                return SetupCheck(
                    "embedding",
                    "model-missing",
                    f"Run setup apply --start-services --pull-model for {config.embedding.model}",
                )
            return SetupCheck("embedding", "ready", config.embedding.model)
        except (OSError, RuntimeError, urllib.error.URLError) as exc:
            return SetupCheck("embedding", "unavailable", str(exc))

    def _write_state(self, config: AppConfig, results: Sequence[SetupCheck]) -> None:
        state_file = config.paths.state_dir / "setup.json"
        payload = {
            "version": __version__,
            "completed_at": datetime.now(UTC).isoformat(),
            "config": str(config.source),
            "checks": [asdict(result) for result in results],
        }
        temporary = state_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(state_file)


def print_checks(checks: Sequence[SetupCheck], *, as_json: bool = False) -> None:
    if as_json:
        print(
            json.dumps(
                {"checks": [asdict(check) for check in checks]}, ensure_ascii=False
            )
        )
        return
    for check in checks:
        requirement = "required" if check.required else "optional"
        print(f"{check.status:18} {check.name:22} {check.detail} ({requirement})")


def checks_ready(checks: Sequence[SetupCheck]) -> bool:
    failures = {
        "error",
        "missing",
        "migration-required",
        "model-missing",
        "unavailable",
    }
    return not any(check.required and check.status in failures for check in checks)
