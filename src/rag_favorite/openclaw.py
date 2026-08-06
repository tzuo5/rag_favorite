"""Safe lifecycle management for the optional OpenClaw MCP registration."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import __version__
from .config import AppConfig, ConfigError
from .mcp_contracts import MCP_TOOL_NAMES
from .mcp_support import mcp_installed

SERVER_NAME = "rag-favorite"
MANAGED_ENV_KEY = "RAG_FAVORITE_OPENCLAW_MANAGED"
BACKUP_SCHEMA_VERSION = 1


class OpenClawError(RuntimeError):
    """A safe OpenClaw integration error suitable for CLI output."""


class OpenClawConflictError(OpenClawError):
    """Raised when registration would replace an unmanaged server."""


@dataclass(frozen=True, slots=True)
class OpenClawDetection:
    installed: bool
    executable: str | None
    version: str | None
    config_path: str | None
    config_exists: bool
    mcp_cli_supported: bool
    product_config_exists: bool
    product_mcp_installed: bool

    @property
    def ready(self) -> bool:
        return all(
            (
                self.installed,
                self.config_exists,
                self.mcp_cli_supported,
                self.product_config_exists,
                self.product_mcp_installed,
            )
        )


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _default_runner(
    command: list[str], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, check=False, **kwargs)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class OpenClawManager:
    def __init__(
        self,
        config: AppConfig,
        *,
        executable: Path | None = None,
        openclaw_config: Path | None = None,
        command_runner: CommandRunner = _default_runner,
        environment: dict[str, str] | None = None,
    ) -> None:
        selected = str(executable) if executable else shutil.which("openclaw")
        self.executable = Path(selected).expanduser().absolute() if selected else None
        self.product_config = config
        self.openclaw_config_override = (
            openclaw_config.expanduser().resolve() if openclaw_config else None
        )
        self._runner = command_runner
        self._environment = dict(environment or os.environ)
        if self.openclaw_config_override:
            self._environment["OPENCLAW_CONFIG_PATH"] = str(
                self.openclaw_config_override
            )

    def detect(self) -> OpenClawDetection:
        if self.executable is None or not self.executable.is_file():
            return OpenClawDetection(
                installed=False,
                executable=str(self.executable) if self.executable else None,
                version=None,
                config_path=(
                    str(self.openclaw_config_override)
                    if self.openclaw_config_override
                    else None
                ),
                config_exists=bool(
                    self.openclaw_config_override
                    and self.openclaw_config_override.is_file()
                ),
                mcp_cli_supported=False,
                product_config_exists=self.product_config.source.is_file(),
                product_mcp_installed=mcp_installed(),
            )
        version_result = self._run(["--version"], check=False)
        help_result = self._run(["mcp", "--help"], check=False)
        config_path: Path | None = None
        if self.openclaw_config_override:
            config_path = self.openclaw_config_override
        else:
            path_result = self._run(["config", "file"], check=False)
            if path_result.returncode == 0 and path_result.stdout.strip():
                config_path = Path(path_result.stdout.strip()).expanduser().resolve()
        return OpenClawDetection(
            installed=True,
            executable=str(self.executable),
            version=(
                version_result.stdout.strip() or version_result.stderr.strip() or None
            ),
            config_path=str(config_path) if config_path else None,
            config_exists=bool(config_path and config_path.is_file()),
            mcp_cli_supported=(
                help_result.returncode == 0 and "mcp.servers" in help_result.stdout
            ),
            product_config_exists=self.product_config.source.is_file(),
            product_mcp_installed=mcp_installed(),
        )

    def desired_server(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "command": str(Path(sys.executable).absolute()),
            "args": ["-m", "rag_favorite.mcp_server"],
            "env": {
                "RAG_FAVORITE_CONFIG": str(self.product_config.source),
                MANAGED_ENV_KEY: "1",
            },
            "transport": "stdio",
            "connectionTimeoutMs": 30_000,
            "requestTimeoutMs": 180_000,
            "supportsParallelToolCalls": True,
            "toolFilter": {"include": list(MCP_TOOL_NAMES)},
        }

    def plan(self, *, replace: bool = False) -> dict[str, Any]:
        self._require_ready()
        current = self._show_server()
        desired = self._registration_target(current, replace=replace)
        if current is None:
            action = "install"
        elif current == desired:
            action = "none"
        elif self._is_managed(current):
            action = "update"
        elif replace:
            action = "replace"
        else:
            action = "conflict"
        return {
            "action": action,
            "server": SERVER_NAME,
            "managed": self._is_managed(current),
            "config_path": str(self._config_path()),
            "will_backup": action in {"install", "update", "replace"},
            "will_probe": action in {"install", "update", "replace"},
            "tools": list(MCP_TOOL_NAMES),
        }

    def status(self, *, probe: bool = False) -> dict[str, Any]:
        detection = self.detect()
        if not detection.ready:
            return {
                "ready": False,
                "detection": asdict(detection),
                "registered": False,
                "managed": False,
                "matches": False,
                "probe_ok": False,
            }
        current = self._show_server()
        managed = self._is_managed(current)
        target = self._registration_target(current, replace=False)
        matches = current == target if current is not None else False
        probe_ok = self._probe() if probe and current is not None else None
        return {
            "ready": bool(current and managed and matches and probe_ok is not False),
            "detection": asdict(detection),
            "registered": current is not None,
            "managed": managed,
            "matches": matches,
            "enabled": bool(current and current.get("enabled", True)),
            "probe_ok": probe_ok,
            "tools": list(MCP_TOOL_NAMES),
        }

    def install(
        self,
        *,
        replace: bool = False,
        probe: bool = True,
    ) -> dict[str, Any]:
        self._require_ready()
        with self._installation_lock():
            current = self._show_server()
            target = self._registration_target(current, replace=replace)
            if current == target:
                return {
                    "action": "none",
                    "server": SERVER_NAME,
                    "backup": None,
                    "probe_ok": None,
                    "reload_ok": None,
                }
            if current is not None and not self._is_managed(current) and not replace:
                raise OpenClawConflictError(
                    f"OpenClaw already has an unmanaged {SERVER_NAME!r} server. "
                    "Use --replace only after reviewing the existing entry."
                )
            action = (
                "install"
                if current is None
                else ("update" if self._is_managed(current) else "replace")
            )
            backup = self._create_backup(action)
            try:
                self._run(
                    [
                        "mcp",
                        "set",
                        SERVER_NAME,
                        json.dumps(target, separators=(",", ":")),
                    ]
                )
                self._run(["config", "validate", "--json"])
                probe_ok = self._probe() if probe else None
                if probe and not probe_ok:
                    raise OpenClawError(
                        "OpenClaw MCP probe returned an invalid tool set."
                    )
            except Exception:
                self._restore_snapshot(backup, validate=True)
                raise
            after_hash = self._config_hash()
            self._finish_backup(backup, after_hash)
            reload_ok = self._reload()
            return {
                "action": action,
                "server": SERVER_NAME,
                "backup": str(backup),
                "probe_ok": probe_ok,
                "reload_ok": reload_ok,
            }

    def uninstall(self, *, force: bool = False) -> dict[str, Any]:
        self._require_openclaw()
        with self._installation_lock():
            current = self._show_server()
            if current is None:
                return {
                    "action": "none",
                    "server": SERVER_NAME,
                    "backup": None,
                    "reload_ok": None,
                }
            if not self._is_managed(current) and not force:
                raise OpenClawConflictError(
                    f"Refusing to remove unmanaged OpenClaw server {SERVER_NAME!r}."
                )
            backup = self._create_backup("uninstall")
            before_hash = self._config_hash()
            action = "uninstall"
            try:
                self._run(["mcp", "unset", SERVER_NAME])
                self._run(["config", "validate", "--json"])
            except OpenClawError:
                if self._config_hash() != before_hash:
                    self._restore_snapshot(backup, validate=True)
                    raise
                try:
                    self._run(["mcp", "configure", SERVER_NAME, "--disable"])
                    self._run(["config", "validate", "--json"])
                    action = "disable"
                except Exception:
                    if self._config_hash() != before_hash:
                        self._restore_snapshot(backup, validate=True)
                    raise
            after_hash = self._config_hash()
            self._finish_backup(backup, after_hash)
            return {
                "action": action,
                "server": SERVER_NAME,
                "backup": str(backup),
                "reload_ok": self._reload(),
            }

    def list_backups(self) -> tuple[dict[str, Any], ...]:
        root = self._backup_root()
        if not root.is_dir():
            return ()
        records: list[dict[str, Any]] = []
        for path in sorted(root.glob("*.metadata.json"), reverse=True):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                value["metadata_path"] = str(path)
                records.append(value)
        return tuple(records)

    def restore(self, metadata_path: Path, *, force: bool = False) -> dict[str, Any]:
        root = self._backup_root().resolve()
        selected = metadata_path.expanduser().resolve()
        if not selected.is_relative_to(root) or not selected.name.endswith(
            ".metadata.json"
        ):
            raise OpenClawError(
                "Backup metadata must come from the managed backup directory."
            )
        try:
            record = json.loads(selected.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OpenClawError("Unable to read OpenClaw backup metadata.") from exc
        if record.get("schema_version") != BACKUP_SCHEMA_VERSION:
            raise OpenClawError("Unsupported OpenClaw backup metadata version.")
        component = str(record.get("component") or record.get("server") or SERVER_NAME)
        self._require_restore_target(component)
        if Path(str(record.get("config_path", ""))).resolve() != self._config_path():
            raise OpenClawError("Backup belongs to a different OpenClaw configuration.")
        with self._installation_lock():
            expected_current = record.get("after_sha256")
            if not force and (
                not isinstance(expected_current, str)
                or self._config_hash() != expected_current
            ):
                raise OpenClawConflictError(
                    "OpenClaw configuration changed after this backup; use --force "
                    "only if replacing those later changes is intentional."
                )
            self._restore_snapshot(selected, validate=True)
            result = {
                "action": "restore",
                "component": component,
                "backup": str(selected),
            }
            if component == SERVER_NAME:
                result.update({"server": SERVER_NAME, "reload_ok": self._reload()})
            else:
                result.update({"restart_required": True})
            return result

    def _registration_target(
        self, current: dict[str, Any] | None, *, replace: bool
    ) -> dict[str, Any]:
        desired = self.desired_server()
        if current is None or replace or not self._is_managed(current):
            return desired
        merged = dict(current)
        merged.update(desired)
        current_env = current.get("env")
        merged["env"] = {
            **(current_env if isinstance(current_env, dict) else {}),
            **desired["env"],
        }
        return merged

    def _is_managed(self, server: dict[str, Any] | None) -> bool:
        if not isinstance(server, dict):
            return False
        environment = server.get("env")
        return bool(
            isinstance(environment, dict)
            and environment.get(MANAGED_ENV_KEY) == "1"
            and server.get("args") == ["-m", "rag_favorite.mcp_server"]
        )

    def _show_server(self) -> dict[str, Any] | None:
        result = self._run(["mcp", "show", SERVER_NAME, "--json"], check=False)
        if result.returncode != 0:
            if "No MCP server named" in result.stderr:
                return None
            raise OpenClawError("Unable to inspect the OpenClaw MCP configuration.")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise OpenClawError(
                "OpenClaw returned invalid MCP configuration data."
            ) from exc
        if not isinstance(value, dict):
            raise OpenClawError("OpenClaw returned invalid MCP configuration data.")
        return value

    def _probe(self) -> bool:
        result = self._run(["mcp", "probe", SERVER_NAME, "--json"])
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise OpenClawError("OpenClaw MCP probe returned invalid data.") from exc
        expected = {f"{SERVER_NAME}__{name}" for name in MCP_TOOL_NAMES}
        tools = payload.get("tools") if isinstance(payload, dict) else None
        diagnostics = payload.get("diagnostics") if isinstance(payload, dict) else None
        return isinstance(tools, list) and set(tools) == expected and not diagnostics

    def _reload(self) -> bool:
        result = self._run(["mcp", "reload"], check=False)
        return result.returncode == 0

    def _require_ready(self) -> None:
        detection = self.detect()
        if not detection.installed:
            raise OpenClawError("OpenClaw is not installed or is not executable.")
        if not detection.mcp_cli_supported:
            raise OpenClawError(
                "This OpenClaw version does not provide MCP management."
            )
        if not detection.config_exists:
            raise OpenClawError(
                "OpenClaw configuration is missing; run openclaw onboard."
            )
        if not detection.product_config_exists:
            raise ConfigError("rag-favorite configuration is missing; run setup first.")
        if not detection.product_mcp_installed:
            raise OpenClawError(
                "MCP support is missing; install rag-favorite with the mcp extra."
            )

    def _require_openclaw(self) -> None:
        detection = self.detect()
        if not detection.installed or not detection.mcp_cli_supported:
            raise OpenClawError("A compatible OpenClaw installation is required.")
        if not detection.config_exists:
            raise OpenClawError("OpenClaw configuration is missing.")

    def _require_restore_target(self, component: str) -> None:
        detection = self.detect()
        if not detection.installed or not detection.config_exists:
            raise OpenClawError("A configured OpenClaw installation is required.")
        if component == SERVER_NAME and not detection.mcp_cli_supported:
            raise OpenClawError("A compatible OpenClaw MCP installation is required.")

    def _config_path(self) -> Path:
        if self.openclaw_config_override:
            selected = self.openclaw_config_override
        else:
            result = self._run(["config", "file"])
            selected = Path(result.stdout.strip()).expanduser().resolve()
        if not selected.is_file() or selected.is_symlink():
            raise OpenClawError("OpenClaw configuration must be a regular file.")
        metadata = selected.stat()
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise OpenClawError("OpenClaw configuration has an unexpected owner.")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise OpenClawError("OpenClaw configuration permissions are too broad.")
        return selected

    def _config_hash(self) -> str:
        return _sha256_bytes(self._config_path().read_bytes())

    def _backup_root(self) -> Path:
        return self.product_config.paths.state_dir / "openclaw-backups"

    def _create_backup(self, operation: str, *, component: str = SERVER_NAME) -> Path:
        self._run(["config", "validate", "--json"])
        config_path = self._config_path()
        root = self._backup_root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        identifier = (
            datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        )
        snapshot = root / f"{identifier}.openclaw.json"
        source = config_path.read_bytes()
        snapshot.write_bytes(source)
        snapshot.chmod(0o600)
        metadata = root / f"{identifier}.metadata.json"
        record = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "operation": operation,
            "component": component,
            "config_path": str(config_path),
            "snapshot_path": str(snapshot),
            "before_sha256": _sha256_bytes(source),
            "after_sha256": None,
            "product_version": __version__,
        }
        if component == SERVER_NAME:
            record["server"] = SERVER_NAME
        self._write_json(metadata, record)
        return metadata

    def _finish_backup(self, metadata: Path, after_hash: str) -> None:
        record = json.loads(metadata.read_text(encoding="utf-8"))
        record["after_sha256"] = after_hash
        self._write_json(metadata, record)

    def _restore_snapshot(self, metadata: Path, *, validate: bool) -> None:
        try:
            metadata_stat = metadata.stat()
            if stat.S_IMODE(metadata_stat.st_mode) & 0o077:
                raise OpenClawError("OpenClaw backup metadata permissions are unsafe.")
            if hasattr(os, "getuid") and metadata_stat.st_uid != os.getuid():
                raise OpenClawError("OpenClaw backup metadata owner is invalid.")
            record = json.loads(metadata.read_text(encoding="utf-8"))
            snapshot = Path(str(record["snapshot_path"])).expanduser().resolve()
            if not snapshot.is_relative_to(self._backup_root().resolve()):
                raise OpenClawError("OpenClaw backup snapshot path is unsafe.")
            snapshot_stat = snapshot.stat()
            if stat.S_IMODE(snapshot_stat.st_mode) & 0o077:
                raise OpenClawError("OpenClaw backup snapshot permissions are unsafe.")
            if hasattr(os, "getuid") and snapshot_stat.st_uid != os.getuid():
                raise OpenClawError("OpenClaw backup snapshot owner is invalid.")
            source = snapshot.read_bytes()
        except (OSError, KeyError, json.JSONDecodeError) as exc:
            raise OpenClawError("Unable to restore the OpenClaw backup.") from exc
        if _sha256_bytes(source) != record.get("before_sha256"):
            raise OpenClawError("OpenClaw backup checksum verification failed.")
        destination = Path(str(record.get("config_path", ""))).expanduser().resolve()
        if destination != self._config_path():
            raise OpenClawError("OpenClaw backup targets a different configuration.")
        recovery = destination.with_name(destination.name + ".rag-favorite-recovery")
        recovery.write_bytes(destination.read_bytes())
        recovery.chmod(0o600)
        temporary = destination.with_name(destination.name + ".rag-favorite-restore")
        temporary.write_bytes(source)
        temporary.chmod(0o600)
        os.replace(temporary, destination)
        try:
            if validate:
                self._run(["config", "validate", "--json"])
        except Exception:
            os.replace(recovery, destination)
            raise
        recovery.unlink(missing_ok=True)

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)

    @contextmanager
    def _installation_lock(self) -> Iterator[None]:
        lock_dir = self.product_config.paths.state_dir
        lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = lock_dir / "openclaw-registration.lock"
        deadline = time.monotonic() + 10
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(
                    lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                if self._remove_stale_lock(lock_path):
                    continue
                if time.monotonic() >= deadline:
                    raise OpenClawError(
                        "Another rag-favorite OpenClaw operation is in progress."
                    )
                time.sleep(0.1)
        try:
            os.write(descriptor, f"{os.getpid()}\n".encode())
            os.close(descriptor)
            descriptor = None
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            lock_path.unlink(missing_ok=True)

    def _remove_stale_lock(self, path: Path) -> bool:
        try:
            raw_pid = path.read_text(encoding="utf-8").strip()
            pid = int(raw_pid)
        except (OSError, ValueError):
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            path.unlink(missing_ok=True)
            return True
        except PermissionError:
            return False
        return False

    def _run(
        self,
        arguments: list[str],
        *,
        check: bool = True,
        timeout: int = 180,
    ) -> subprocess.CompletedProcess[str]:
        if self.executable is None:
            raise OpenClawError("OpenClaw is not installed.")
        try:
            result = self._runner(
                [str(self.executable), *arguments],
                capture_output=True,
                env=self._environment,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OpenClawError("Unable to run the OpenClaw CLI.") from exc
        if check and result.returncode != 0:
            raise OpenClawError(f"OpenClaw command failed: {' '.join(arguments[:2])}.")
        return result
