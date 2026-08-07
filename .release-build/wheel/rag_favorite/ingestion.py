"""Portable lifecycle for the optional media ingestion and Telegram adapter."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import AppConfig, ConfigError
from .openclaw import OpenClawConflictError, OpenClawError, OpenClawManager
from .setup import LAUNCHD_LABELS, SetupManager, SetupOptions

PLUGIN_ID = "video-knowledge-ingest"
CAPABILITY_TOOL = "video_ingestion_capabilities"
REQUIRED_COMMANDS = frozenset(
    {
        "video_status",
        "video_pause",
        "video_resume",
        "video_cancel",
        "video_batch_status",
        "video_batch_pause",
        "video_batch_resume",
        "video_batch_cancel",
        "video_login",
    }
)
REQUIRED_HOOKS = frozenset({"inbound_claim", "message_received", "before_agent_reply"})
DEPENDENCY_IMPORTS = (
    "yaml",
    "psycopg",
    "yt_dlp",
    "openai",
    "backend.ingestion.cli",
)


class IngestionError(RuntimeError):
    """A safe ingestion lifecycle error suitable for CLI output."""


@dataclass(frozen=True, slots=True)
class IngestionDetection:
    project_dir: str | None
    source_ready: bool
    product_config_exists: bool
    env_exists: bool
    env_private: bool
    venv_exists: bool
    dependencies_ready: bool
    ffmpeg_ready: bool
    plugin_source_ready: bool
    telegram_configured: bool

    @property
    def ready(self) -> bool:
        return all(
            (
                self.source_ready,
                self.product_config_exists,
                self.env_exists,
                self.env_private,
                self.venv_exists,
                self.dependencies_ready,
                self.ffmpeg_ready,
            )
        )


def _default_runner(
    command: list[str], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, check=False, **kwargs)


def _env_value(value: str | Path) -> str:
    raw = str(value)
    return '"' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _read_env_flags(path: Path) -> dict[str, bool]:
    flags = {"token": False, "users": False}
    if not path.is_file():
        return flags
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return flags
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    flags["token"] = bool(values.get("TELEGRAM_BOT_TOKEN"))
    flags["users"] = bool(values.get("TELEGRAM_ALLOWED_USER_IDS"))
    return flags


class IngestionManager:
    def __init__(
        self,
        config: AppConfig,
        *,
        project_dir: Path | None = None,
        command_runner: Any = _default_runner,
    ) -> None:
        self.config = config
        self.project_dir = self._resolve_project_dir(project_dir)
        self._runner = command_runner

    def detect(self) -> IngestionDetection:
        project = self.project_dir
        source_ready = bool(
            project
            and all(
                path.is_file()
                for path in (
                    project / "requirements.txt",
                    project / "start.py",
                    project / "backend/ingestion/cli.py",
                )
            )
        )
        env_file = project / ".env" if project else None
        venv_python = project / ".venv/bin/python" if project else None
        env_exists = bool(env_file and env_file.is_file() and not env_file.is_symlink())
        env_private = bool(
            env_exists and stat.S_IMODE(env_file.stat().st_mode) & 0o077 == 0
        )
        venv_exists = bool(
            venv_python and venv_python.is_file() and os.access(venv_python, os.X_OK)
        )
        plugin_source_ready = bool(
            project
            and (project / "openclaw-plugin/index.js").is_file()
            and (project / "openclaw-plugin/openclaw.plugin.json").is_file()
        )
        flags = (
            _read_env_flags(env_file) if env_file else {"token": False, "users": False}
        )
        return IngestionDetection(
            project_dir=str(project) if project else None,
            source_ready=source_ready,
            product_config_exists=self.config.source.is_file(),
            env_exists=env_exists,
            env_private=env_private,
            venv_exists=venv_exists,
            dependencies_ready=(
                self._dependencies_ready(venv_python, project)
                if venv_exists and project
                else False
            ),
            ffmpeg_ready=shutil.which("ffmpeg") is not None,
            plugin_source_ready=plugin_source_ready,
            telegram_configured=flags["token"] and flags["users"],
        )

    def plan(
        self,
        *,
        install_dependencies: bool = False,
        render_systemd: bool = False,
        render_launchd: bool = False,
        enable_services: bool = False,
        register_openclaw: bool = False,
        plugin_manager: OpenClawPluginManager | None = None,
    ) -> dict[str, Any]:
        if render_systemd and render_launchd:
            raise ConfigError("Choose either --render-systemd or --render-launchd.")
        if enable_services and not (render_systemd or render_launchd):
            raise ConfigError(
                "--enable-services requires --render-systemd or --render-launchd."
            )
        detection = self.detect()
        changes: list[str] = []
        if not detection.env_exists:
            changes.append("create-private-env")
        elif not detection.env_private:
            changes.append("restrict-env-permissions")
        if not self._directories_ready():
            changes.append("ensure-runtime-directories")
        if install_dependencies:
            if not detection.venv_exists:
                changes.append("create-virtualenv")
            if not detection.dependencies_ready:
                changes.append("install-python-dependencies")
        if render_systemd:
            changes.append("render-user-systemd")
        if render_launchd:
            changes.append("render-user-launchd")
        if enable_services:
            changes.append("enable-user-services")
        plugin_plan: dict[str, Any] | None = None
        if register_openclaw:
            manager = plugin_manager or self.openclaw_plugin_manager()
            plugin_plan = manager.plan()
            if plugin_plan["action"] != "none":
                changes.append(f"openclaw-plugin-{plugin_plan['action']}")
        return {
            "action": "none" if not changes else "install",
            "project_dir": detection.project_dir,
            "changes": changes,
            "network_required": bool(
                install_dependencies and not detection.dependencies_ready
            ),
            "openclaw": plugin_plan,
            "detection": asdict(detection),
        }

    def install(
        self,
        *,
        install_dependencies: bool = False,
        render_systemd: bool = False,
        render_launchd: bool = False,
        enable_services: bool = False,
        register_openclaw: bool = False,
        plugin_manager: OpenClawPluginManager | None = None,
    ) -> dict[str, Any]:
        plan = self.plan(
            install_dependencies=install_dependencies,
            render_systemd=render_systemd,
            render_launchd=render_launchd,
            enable_services=enable_services,
            register_openclaw=register_openclaw,
            plugin_manager=plugin_manager,
        )
        project = self._require_source()
        if not self.config.source.is_file():
            raise ConfigError("rag-favorite configuration is missing; run setup first.")
        steps: list[str] = []
        env_file = project / ".env"
        if not env_file.exists():
            self._write_environment(env_file)
            steps.append("created-private-env")
        else:
            if env_file.is_symlink() or not env_file.is_file():
                raise IngestionError("The ingestion .env path must be a regular file.")
            if stat.S_IMODE(env_file.stat().st_mode) != 0o600:
                env_file.chmod(0o600)
                steps.append("restricted-env-permissions")
        if self._ensure_directories():
            steps.append("ensured-runtime-directories")
        if install_dependencies:
            venv_python = project / ".venv/bin/python"
            dependencies_ready = (
                venv_python.is_file()
                and os.access(venv_python, os.X_OK)
                and self._dependencies_ready(venv_python, project)
            )
            if not venv_python.is_file():
                self._run(
                    [sys.executable, "-m", "venv", str(project / ".venv")],
                    cwd=project,
                    timeout=300,
                )
                steps.append("created-virtualenv")
            if not dependencies_ready:
                self._run(
                    [
                        str(venv_python),
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "--requirement",
                        str(project / "requirements.txt"),
                    ],
                    cwd=project,
                    timeout=1800,
                )
            if not self._dependencies_ready(venv_python, project):
                raise IngestionError(
                    "The ingestion dependency probe failed after install."
                )
            if not dependencies_ready:
                steps.append("installed-python-dependencies")
        if render_systemd:
            SetupManager(self.config.source).apply(
                SetupOptions(
                    migrate_database=False,
                    render_systemd=True,
                    enable_services=enable_services,
                    ingestion_dir=project,
                )
            )
            steps.append(
                "enabled-user-services" if enable_services else "rendered-user-systemd"
            )
        if render_launchd:
            SetupManager(self.config.source).apply(
                SetupOptions(
                    migrate_database=False,
                    render_launchd=True,
                    enable_services=enable_services,
                    ingestion_dir=project,
                )
            )
            steps.append(
                "enabled-user-services" if enable_services else "rendered-user-launchd"
            )
        plugin_result: dict[str, Any] | None = None
        if register_openclaw:
            manager = plugin_manager or self.openclaw_plugin_manager()
            plugin_result = manager.install()
            if plugin_result["action"] != "none":
                steps.append(f"openclaw-plugin-{plugin_result['action']}")
        detection = self.detect()
        return {
            "action": "none" if not steps else "install",
            "project_dir": str(project),
            "steps": steps,
            "ready": detection.ready,
            "telegram_configured": detection.telegram_configured,
            "openclaw": plugin_result,
            "planned": plan["changes"],
        }

    def status(
        self,
        *,
        inspect_openclaw: bool = False,
        plugin_manager: OpenClawPluginManager | None = None,
    ) -> dict[str, Any]:
        detection = self.detect()
        plugin_status: dict[str, Any] | None = None
        if inspect_openclaw:
            manager = plugin_manager or self.openclaw_plugin_manager()
            plugin_status = manager.status()
        return {
            "ready": detection.ready
            and (
                not inspect_openclaw or bool(plugin_status and plugin_status["ready"])
            ),
            "detection": asdict(detection),
            "openclaw": plugin_status,
        }

    def uninstall(
        self,
        *,
        unregister_openclaw: bool = False,
        disable_services: bool = False,
        force: bool = False,
        plugin_manager: OpenClawPluginManager | None = None,
    ) -> dict[str, Any]:
        steps: list[str] = []
        if disable_services:
            if sys.platform == "darwin":
                if not shutil.which("launchctl"):
                    raise IngestionError(
                        "launchctl is required for --disable-services."
                    )
                domain = f"gui/{os.getuid()}"
                for label in LAUNCHD_LABELS:
                    self._run(
                        ["launchctl", "bootout", f"{domain}/{label}"],
                        check=False,
                        timeout=30,
                    )
            else:
                if not shutil.which("systemctl"):
                    raise IngestionError(
                        "systemctl is required for --disable-services."
                    )
                units = (
                    "video-ingestion-worker.service",
                    "video-author-discovery-worker.service",
                    "video-batch-notification-worker.service",
                    "video-ingestion-cleanup.timer",
                    "xhs-session-check.timer",
                )
                self._run(
                    ["systemctl", "--user", "disable", "--now", *units],
                    timeout=120,
                )
            steps.append("disabled-user-services")
        plugin_result: dict[str, Any] | None = None
        if unregister_openclaw:
            manager = plugin_manager or self.openclaw_plugin_manager()
            plugin_result = manager.uninstall(force=force)
            if plugin_result["action"] != "none":
                steps.append(f"openclaw-plugin-{plugin_result['action']}")
        return {
            "action": "none" if not steps else "uninstall",
            "steps": steps,
            "openclaw": plugin_result,
            "preserved": [".env", ".venv", "knowledge", "database", "media"],
        }

    def openclaw_plugin_manager(
        self,
        *,
        executable: Path | None = None,
        openclaw_config: Path | None = None,
        command_runner: Any | None = None,
        environment: dict[str, str] | None = None,
    ) -> OpenClawPluginManager:
        project = self._require_source()
        return OpenClawPluginManager(
            self.config,
            project,
            executable=executable,
            openclaw_config=openclaw_config,
            command_runner=command_runner or _default_runner,
            environment=environment,
        )

    def _resolve_project_dir(self, selected: Path | None) -> Path | None:
        if selected:
            return selected.expanduser().resolve()
        configured = os.getenv("RAG_FAVORITE_INGESTION_DIR")
        candidates = [
            Path(configured).expanduser() if configured else None,
            Path.cwd() / "ingestion",
            Path.cwd(),
            Path(__file__).resolve().parents[2] / "ingestion",
        ]
        for candidate in candidates:
            if candidate and (candidate / "backend/ingestion/cli.py").is_file():
                return candidate.resolve()
        return None

    def _require_source(self) -> Path:
        if not self.detect().source_ready or self.project_dir is None:
            raise IngestionError(
                "Ingestion source is missing; pass --project-dir pointing to the repository ingestion directory."
            )
        return self.project_dir

    def _write_environment(self, path: Path) -> None:
        general = self.config.collections.get("general")
        cooking = self.config.collections.get("cooking")
        lines = [
            "# Generated by rag-favorite Phase 5. Add secrets locally; never commit this file.",
            f"RAG_FAVORITE_CONFIG={_env_value(self.config.source)}",
            "TELEGRAM_BOT_TOKEN=",
            "TELEGRAM_ALLOWED_USER_IDS=",
            "OPENAI_API_KEY=",
            "OPENAI_BASE_URL=",
            "OPENAI_MODEL=",
            f"VIDEO_TEMP_ROOT={_env_value(self.config.paths.cache_dir / 'ingestion/jobs')}",
            f"VIDEO_STAGING_ROOT={_env_value(self.config.paths.state_dir / 'ingestion/staging')}",
            f"VIDEO_DB_ENV={_env_value(self.config.database.credentials_file or self.config.paths.secrets_file)}",
            f"OPENCLAW_MEDIA_ROOT={_env_value(self.config.paths.data_dir / 'openclaw-media')}",
        ]
        if general:
            lines.append(
                f"MAIN_KB_MARKDOWN_DIR={_env_value(general.path / 'video-transcripts')}"
            )
        if cooking:
            lines.append(
                f"COOKING_KB_MARKDOWN_DIR={_env_value(cooking.path / 'video-transcripts')}"
            )
        lines.extend(
            [
                "AUTHOR_BATCH_YOUTUBE_DISCOVERY_ENABLED=false",
                "AUTHOR_BATCH_YOUTUBE_EXECUTION_ENABLED=false",
                "AUTHOR_BATCH_BILIBILI_ENABLED=false",
                "AUTHOR_BATCH_XIAOHONGSHU_ENABLED=false",
                "BILIBILI_SESSION_ENABLED=false",
                "XIAOHONGSHU_SESSION_ENABLED=false",
            ]
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".env.rag-favorite-tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)

    def _runtime_directories(self) -> tuple[Path, ...]:
        project = self._require_source()
        return (
            project / "secrets",
            self.config.paths.cache_dir / "ingestion/jobs",
            self.config.paths.state_dir / "ingestion/staging",
            self.config.paths.data_dir / "openclaw-media/inbound",
        )

    def _collection_directories(self) -> tuple[Path, ...]:
        return tuple(
            collection.path / "video-transcripts"
            for collection in self.config.collections.values()
            if not collection.read_only
        )

    def _directories_ready(self) -> bool:
        return all(
            directory.is_dir() and stat.S_IMODE(directory.stat().st_mode) & 0o077 == 0
            for directory in self._runtime_directories()
        ) and all(directory.is_dir() for directory in self._collection_directories())

    def _ensure_directories(self) -> bool:
        changed = False
        for directory in self._runtime_directories():
            if (
                not directory.is_dir()
                or stat.S_IMODE(directory.stat().st_mode) != 0o700
            ):
                changed = True
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        for directory in self._collection_directories():
            if not directory.is_dir():
                changed = True
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return changed

    def _dependencies_ready(self, python: Path, project: Path) -> bool:
        imports = "; ".join(f"import {name}" for name in DEPENDENCY_IMPORTS)
        result = self._run(
            [str(python), "-c", imports],
            cwd=project,
            check=False,
            timeout=60,
        )
        return result.returncode == 0

    def _run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        timeout: int = 180,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self._runner(
                command,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise IngestionError("Unable to run an ingestion setup command.") from exc
        if check and result.returncode != 0:
            raise IngestionError(f"Ingestion command failed: {' '.join(command[:3])}.")
        return result


class OpenClawPluginManager(OpenClawManager):
    """Guard the optional OpenClaw Telegram ingestion plugin registration."""

    def __init__(self, config: AppConfig, project_dir: Path, **kwargs: Any) -> None:
        super().__init__(config, **kwargs)
        self.project_dir = project_dir.expanduser().resolve()
        self.plugin_dir = (self.project_dir / "openclaw-plugin").resolve()
        self.venv_python = (self.project_dir / ".venv/bin/python").resolve()

    def plan(self) -> dict[str, Any]:
        self._require_plugin_plan_ready()
        current = self._inspect(runtime=True)
        if current is None:
            action = "install"
        elif self._matches(current):
            action = "none"
        elif self._is_managed_plugin(current):
            action = "update"
        else:
            action = "conflict"
        return {
            "action": action,
            "plugin": PLUGIN_ID,
            "managed": self._is_managed_plugin(current),
            "config_path": str(self._config_path()),
            "will_backup": action in {"install", "update"},
            "restart_required": action in {"install", "update"},
            "tool": CAPABILITY_TOOL,
            "venv_ready": self.venv_python.is_file(),
        }

    def status(self) -> dict[str, Any]:
        compatible = self._plugins_supported()
        if not compatible:
            return {
                "ready": False,
                "plugin": PLUGIN_ID,
                "installed": False,
                "managed": False,
                "matches": False,
            }
        current = self._inspect(runtime=True)
        managed = self._is_managed_plugin(current)
        matches = self._matches(current) if current else False
        return {
            "ready": bool(current and managed and matches),
            "plugin": PLUGIN_ID,
            "installed": current is not None,
            "managed": managed,
            "matches": matches,
            "enabled": bool(current and current.get("plugin", {}).get("enabled")),
            "tool": CAPABILITY_TOOL,
            "restart_required": False,
        }

    def install(self) -> dict[str, Any]:
        self._require_plugin_ready()
        with self._installation_lock():
            current = self._inspect(runtime=True)
            if current is not None and not self._is_managed_plugin(current):
                raise OpenClawConflictError(
                    f"OpenClaw already has an unmanaged {PLUGIN_ID!r} plugin."
                )
            if current is not None and self._matches(current):
                return {
                    "action": "none",
                    "plugin": PLUGIN_ID,
                    "backup": None,
                    "probe_ok": None,
                    "restart_required": False,
                }
            action = "install" if current is None else "update"
            backup = self._create_backup(f"plugin-{action}", component=PLUGIN_ID)
            installed_new = current is None
            try:
                if installed_new:
                    self._run(["plugins", "install", "--link", str(self.plugin_dir)])
                self._set_plugin_config()
                self._run(["config", "validate", "--json"])
                probe_ok = self._matches(self._inspect(runtime=True))
                if not probe_ok:
                    raise OpenClawError(
                        "OpenClaw loaded an incomplete ingestion plugin contract."
                    )
            except Exception:
                if installed_new:
                    self._run(
                        ["plugins", "uninstall", PLUGIN_ID, "--force"], check=False
                    )
                self._restore_snapshot(backup, validate=True)
                raise
            self._finish_backup(backup, self._config_hash())
            return {
                "action": action,
                "plugin": PLUGIN_ID,
                "backup": str(backup),
                "probe_ok": True,
                "restart_required": True,
            }

    def uninstall(self, *, force: bool = False) -> dict[str, Any]:
        self._require_plugin_cli()
        with self._installation_lock():
            current = self._inspect(runtime=True)
            if current is None:
                return {
                    "action": "none",
                    "plugin": PLUGIN_ID,
                    "backup": None,
                    "restart_required": False,
                }
            if not self._is_managed_plugin(current) and not force:
                raise OpenClawConflictError(
                    f"Refusing to remove unmanaged OpenClaw plugin {PLUGIN_ID!r}."
                )
            backup = self._create_backup("plugin-uninstall", component=PLUGIN_ID)
            before_hash = self._config_hash()
            action = "uninstall"
            try:
                self._run(["plugins", "uninstall", PLUGIN_ID, "--force"])
                self._run(["config", "validate", "--json"])
            except OpenClawError:
                if self._config_hash() != before_hash:
                    self._restore_snapshot(backup, validate=True)
                    raise
                self._run(["plugins", "disable", PLUGIN_ID])
                self._run(["config", "validate", "--json"])
                action = "disable"
            self._finish_backup(backup, self._config_hash())
            return {
                "action": action,
                "plugin": PLUGIN_ID,
                "backup": str(backup),
                "restart_required": True,
            }

    def _set_plugin_config(self) -> None:
        self._run(["plugins", "enable", PLUGIN_ID])
        value = json.dumps(
            {"python": str(self.venv_python), "projectDir": str(self.project_dir)},
            separators=(",", ":"),
        )
        self._run(
            [
                "config",
                "set",
                f"plugins.entries.{PLUGIN_ID}.config",
                value,
                "--strict-json",
            ]
        )
        self._run(
            [
                "config",
                "set",
                f"plugins.entries.{PLUGIN_ID}.hooks.allowConversationAccess",
                "true",
                "--strict-json",
            ]
        )

    def _plugins_supported(self) -> bool:
        if self.executable is None or not self.executable.is_file():
            return False
        result = self._run(["plugins", "--help"], check=False)
        return result.returncode == 0 and "Manage OpenClaw plugins" in result.stdout

    def _require_plugin_cli(self) -> None:
        if not self._plugins_supported():
            raise OpenClawError(
                "A compatible OpenClaw installation with plugin management is required."
            )
        if not self._config_path().is_file():
            raise OpenClawError("OpenClaw configuration is missing.")

    def _require_plugin_ready(self) -> None:
        self._require_plugin_plan_ready()
        if not self.venv_python.is_file():
            raise IngestionError(
                "The ingestion virtual environment is missing; install dependencies first."
            )

    def _require_plugin_plan_ready(self) -> None:
        self._require_plugin_cli()
        if not self.product_config.source.is_file():
            raise ConfigError("rag-favorite configuration is missing; run setup first.")
        if not self.plugin_dir.joinpath("index.js").is_file():
            raise IngestionError("The OpenClaw ingestion plugin source is missing.")

    def _inspect(self, *, runtime: bool) -> dict[str, Any] | None:
        arguments = ["plugins", "inspect", PLUGIN_ID]
        if runtime:
            arguments.append("--runtime")
        arguments.append("--json")
        result = self._run(arguments, check=False)
        if result.returncode != 0:
            text = (result.stderr + result.stdout).lower()
            if "not found" in text or "unknown plugin" in text:
                return None
            raise OpenClawError("Unable to inspect the OpenClaw ingestion plugin.")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise OpenClawError(
                "OpenClaw returned invalid plugin inspection data."
            ) from exc
        if not isinstance(payload, dict):
            raise OpenClawError("OpenClaw returned invalid plugin inspection data.")
        return payload

    def _entry(self) -> dict[str, Any] | None:
        result = self._run(
            ["config", "get", f"plugins.entries.{PLUGIN_ID}", "--json"],
            check=False,
        )
        if result.returncode != 0:
            return None
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _is_managed_plugin(self, current: dict[str, Any] | None) -> bool:
        if not current:
            return False
        install = current.get("install")
        source = install.get("sourcePath") if isinstance(install, dict) else None
        if not isinstance(source, str):
            plugin = current.get("plugin")
            source = plugin.get("rootDir") if isinstance(plugin, dict) else None
        try:
            return bool(
                source and Path(source).expanduser().resolve() == self.plugin_dir
            )
        except OSError:
            return False

    def _matches(self, current: dict[str, Any] | None) -> bool:
        if not current or not self._is_managed_plugin(current):
            return False
        plugin = current.get("plugin")
        if not isinstance(plugin, dict) or not plugin.get("enabled"):
            return False
        entry = self._entry()
        if not entry:
            return False
        desired_config = {
            "python": str(self.venv_python),
            "projectDir": str(self.project_dir),
        }
        hooks = entry.get("hooks")
        if entry.get("config") != desired_config or not (
            isinstance(hooks, dict) and hooks.get("allowConversationAccess") is True
        ):
            return False
        tools = (
            set(current.get("tools", [{}])[0].get("names", []))
            if current.get("tools")
            else set()
        )
        commands = set(current.get("commands", []))
        typed_hooks = {
            item.get("name")
            for item in current.get("typedHooks", [])
            if isinstance(item, dict)
        }
        diagnostics = current.get("diagnostics", [])
        errors = [
            item
            for item in diagnostics
            if isinstance(item, dict) and item.get("level") == "error"
        ]
        return (
            CAPABILITY_TOOL in tools
            and REQUIRED_COMMANDS <= commands
            and REQUIRED_HOOKS <= typed_hooks
            and not errors
        )
