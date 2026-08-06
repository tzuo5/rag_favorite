from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from rag_favorite.config import load_config
from rag_favorite.openclaw import (
    MANAGED_ENV_KEY,
    OpenClawConflictError,
    OpenClawError,
    OpenClawManager,
)


def _product_config(tmp_path: Path, monkeypatch: object):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config_file = tmp_path / "product.toml"
    config_file.write_text(
        '[[collections]]\nkey = "notes"\nname = "Notes"\npath = "notes"\n',
        encoding="utf-8",
    )
    return load_config(config_file)


class FakeOpenClaw:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.fail_probe = False
        self.fail_unset = False
        self.commands: list[list[str]] = []

    def __call__(
        self, command: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        arguments = command[1:]
        if arguments == ["--version"]:
            return self._result(command, stdout="OpenClaw 2026.7.1\n")
        if arguments == ["mcp", "--help"]:
            return self._result(command, stdout="Manage OpenClaw mcp.servers config\n")
        if arguments == ["config", "file"]:
            return self._result(command, stdout=str(self.config_path) + "\n")
        if arguments[:3] == ["mcp", "show", "rag-favorite"]:
            server = self._server()
            if server is None:
                return self._result(
                    command,
                    returncode=1,
                    stderr='No MCP server named "rag-favorite".\n',
                )
            return self._result(command, stdout=json.dumps(server))
        if arguments[:3] == ["mcp", "set", "rag-favorite"]:
            self._write_server(json.loads(arguments[3]))
            return self._result(command)
        if arguments[:3] == ["mcp", "probe", "rag-favorite"]:
            tools = (
                []
                if self.fail_probe
                else [
                    "rag-favorite__rag_search",
                    "rag-favorite__rag_status",
                ]
            )
            return self._result(
                command,
                stdout=json.dumps({"tools": tools, "diagnostics": []}),
            )
        if arguments == ["mcp", "unset", "rag-favorite"]:
            if self.fail_unset:
                return self._result(command, returncode=1, stderr="size-drop\n")
            self._write_server(None)
            return self._result(command)
        if arguments == ["mcp", "configure", "rag-favorite", "--disable"]:
            server = self._server()
            assert server is not None
            self._write_server({**server, "enabled": False})
            return self._result(command)
        if arguments in (
            ["config", "validate", "--json"],
            ["mcp", "reload"],
        ):
            return self._result(command, stdout="{}\n")
        raise AssertionError(f"Unexpected OpenClaw command: {arguments}")

    def _payload(self) -> dict[str, Any]:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def _server(self) -> dict[str, Any] | None:
        return self._payload().get("mcp", {}).get("servers", {}).get("rag-favorite")

    def _write_server(self, server: dict[str, Any] | None) -> None:
        payload = self._payload()
        mcp = payload.setdefault("mcp", {})
        servers = mcp.setdefault("servers", {})
        if server is None:
            servers.pop("rag-favorite", None)
        else:
            servers["rag-favorite"] = server
        self.config_path.write_text(json.dumps(payload), encoding="utf-8")
        self.config_path.chmod(0o600)

    @staticmethod
    def _result(
        command: list[str],
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def _manager(tmp_path: Path, monkeypatch: object):
    product = _product_config(tmp_path, monkeypatch)
    executable = tmp_path / "openclaw"
    executable.write_text("test executable\n", encoding="utf-8")
    executable.chmod(0o700)
    openclaw_config = tmp_path / "openclaw.json"
    openclaw_config.write_text(
        json.dumps({"gateway": {"mode": "local"}, "mcp": {"servers": {}}}),
        encoding="utf-8",
    )
    openclaw_config.chmod(0o600)
    runner = FakeOpenClaw(openclaw_config)
    manager = OpenClawManager(
        product,
        executable=executable,
        openclaw_config=openclaw_config,
        command_runner=runner,
    )
    return manager, runner, openclaw_config


def test_detect_and_plan_are_read_only(tmp_path: Path, monkeypatch: object) -> None:
    manager, runner, config_path = _manager(tmp_path, monkeypatch)
    original = config_path.read_bytes()

    detection = manager.detect()
    plan = manager.plan()

    assert detection.ready
    assert plan["action"] == "install"
    assert plan["will_backup"] is True
    assert config_path.read_bytes() == original
    assert not any("set" in command for command in runner.commands)


def test_install_is_probed_backed_up_and_idempotent(
    tmp_path: Path, monkeypatch: object
) -> None:
    manager, runner, config_path = _manager(tmp_path, monkeypatch)

    first = manager.install()
    second = manager.install()
    status = manager.status(probe=True)

    assert first["action"] == "install"
    assert first["probe_ok"] is True
    metadata_path = Path(first["backup"])
    assert metadata_path.stat().st_mode & 0o077 == 0
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert Path(metadata["snapshot_path"]).stat().st_mode & 0o077 == 0
    assert second["action"] == "none"
    assert status["ready"] is True
    server = json.loads(config_path.read_text(encoding="utf-8"))["mcp"]["servers"][
        "rag-favorite"
    ]
    assert server["env"][MANAGED_ENV_KEY] == "1"
    assert server["toolFilter"]["include"] == ["rag_search", "rag_status"]
    assert sum(command[1:3] == ["mcp", "set"] for command in runner.commands) == 1


def test_install_refuses_unmanaged_collision(
    tmp_path: Path, monkeypatch: object
) -> None:
    manager, runner, _config_path = _manager(tmp_path, monkeypatch)
    runner._write_server({"command": "someone-else", "args": []})

    assert manager.plan()["action"] == "conflict"
    with pytest.raises(OpenClawConflictError, match="unmanaged"):
        manager.install()

    assert runner._server() == {"command": "someone-else", "args": []}


def test_probe_failure_rolls_back_exact_config(
    tmp_path: Path, monkeypatch: object
) -> None:
    manager, runner, config_path = _manager(tmp_path, monkeypatch)
    original = config_path.read_bytes()
    runner.fail_probe = True

    with pytest.raises(OpenClawError, match="probe"):
        manager.install()

    assert config_path.read_bytes() == original


def test_uninstall_and_restore_are_targeted(
    tmp_path: Path, monkeypatch: object
) -> None:
    manager, _runner, config_path = _manager(tmp_path, monkeypatch)
    manager.install()

    removed = manager.uninstall()
    assert removed["action"] == "uninstall"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["gateway"] == {"mode": "local"}
    assert "rag-favorite" not in payload["mcp"]["servers"]

    restored = manager.restore(Path(removed["backup"]))
    assert restored["action"] == "restore"
    assert manager.status()["ready"] is True


def test_uninstall_falls_back_to_disable_when_openclaw_guard_rejects_removal(
    tmp_path: Path, monkeypatch: object
) -> None:
    manager, runner, _config_path = _manager(tmp_path, monkeypatch)
    manager.install()
    runner.fail_unset = True

    result = manager.uninstall()

    assert result["action"] == "disable"
    assert runner._server()["enabled"] is False


def test_restore_refuses_later_openclaw_changes(
    tmp_path: Path, monkeypatch: object
) -> None:
    manager, runner, _config_path = _manager(tmp_path, monkeypatch)
    installed = manager.install()
    runner._write_server({**runner._server(), "requestTimeoutMs": 42})

    with pytest.raises(OpenClawConflictError, match="changed after"):
        manager.restore(Path(installed["backup"]))


def test_install_recovers_lock_from_dead_process(
    tmp_path: Path, monkeypatch: object
) -> None:
    manager, _runner, _config_path = _manager(tmp_path, monkeypatch)
    lock_path = manager.product_config.paths.state_dir / "openclaw-registration.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("999999999\n", encoding="utf-8")
    lock_path.chmod(0o600)

    result = manager.install()

    assert result["action"] == "install"
    assert not lock_path.exists()
