from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from rag_favorite.cli import main
from rag_favorite.config import ConfigError, load_config
from rag_favorite.ingestion import (
    CAPABILITY_TOOL,
    PLUGIN_ID,
    IngestionManager,
    OpenClawPluginManager,
)
from rag_favorite.openclaw import OpenClawConflictError, OpenClawError
from rag_favorite.setup import LAUNCHD_LABELS


def _product(tmp_path: Path, monkeypatch: object):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config_file = tmp_path / "config" / "config.toml"
    config_file.parent.mkdir()
    config_file.write_text(
        """
[database]
credentials_file = "secrets.env"

[[collections]]
key = "general"
name = "General"
path = "knowledge/general"

[[collections]]
key = "cooking"
name = "Cooking"
path = "knowledge/cooking"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return load_config(config_file)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "ingestion"
    (project / "backend/ingestion").mkdir(parents=True)
    (project / "backend/ingestion/cli.py").write_text("", encoding="utf-8")
    (project / "requirements.txt").write_text("PyYAML\n", encoding="utf-8")
    (project / "start.py").write_text("", encoding="utf-8")
    plugin = project / "openclaw-plugin"
    plugin.mkdir()
    (plugin / "index.js").write_text("export default {};\n", encoding="utf-8")
    (plugin / "openclaw.plugin.json").write_text("{}\n", encoding="utf-8")
    return project


def _ffmpeg_ready(monkeypatch: object) -> None:
    monkeypatch.setattr(
        "rag_favorite.ingestion.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"ffmpeg", "systemctl"} else None,
    )


def test_plan_is_read_only_and_describes_network_work(
    tmp_path: Path, monkeypatch: object
) -> None:
    product = _product(tmp_path, monkeypatch)
    project = _project(tmp_path)
    _ffmpeg_ready(monkeypatch)
    manager = IngestionManager(product, project_dir=project)

    result = manager.plan(install_dependencies=True, render_systemd=True)

    assert result["action"] == "install"
    assert result["network_required"] is True
    assert "create-private-env" in result["changes"]
    assert "create-virtualenv" in result["changes"]
    assert "render-user-systemd" in result["changes"]
    assert not (project / ".env").exists()
    assert not (project / ".venv").exists()


def test_plan_requires_one_matching_service_manager(
    tmp_path: Path, monkeypatch: object
) -> None:
    product = _product(tmp_path, monkeypatch)
    project = _project(tmp_path)
    manager = IngestionManager(product, project_dir=project)

    with pytest.raises(
        ConfigError, match="either --render-systemd or --render-launchd"
    ):
        manager.plan(render_systemd=True, render_launchd=True)
    with pytest.raises(
        ConfigError, match="requires --render-systemd or --render-launchd"
    ):
        manager.plan(enable_services=True)


def test_install_creates_private_secret_free_environment_and_preserves_it(
    tmp_path: Path, monkeypatch: object
) -> None:
    product = _product(tmp_path, monkeypatch)
    project = _project(tmp_path)
    _ffmpeg_ready(monkeypatch)
    manager = IngestionManager(product, project_dir=project)

    first = manager.install()
    env_file = project / ".env"
    original = env_file.read_text(encoding="utf-8")
    env_file.write_text(original + "CUSTOM_SETTING=keep-me\n", encoding="utf-8")
    env_file.chmod(0o644)
    second = manager.install()

    assert first["action"] == "install"
    assert "TELEGRAM_BOT_TOKEN=\n" in original
    assert "TELEGRAM_ALLOWED_USER_IDS=\n" in original
    assert "your_openai_api_key" not in original
    assert str(product.source) in original
    assert "CUSTOM_SETTING=keep-me" in env_file.read_text(encoding="utf-8")
    assert env_file.stat().st_mode & 0o077 == 0
    assert second["steps"] == ["restricted-env-permissions"]
    assert (product.paths.data_dir / "openclaw-media/inbound").is_dir()

    third = manager.install()
    assert third["action"] == "none"
    assert third["steps"] == []
    assert third["planned"] == []


class DependencyRunner:
    def __init__(self, project: Path) -> None:
        self.project = project
        self.commands: list[list[str]] = []
        self.installed = False

    def __call__(
        self, command: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[1:3] == ["-m", "venv"]:
            python = self.project / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            python.chmod(0o700)
        elif command[1:3] == ["-m", "pip"]:
            self.installed = True
        elif command[1:2] == ["-c"]:
            return subprocess.CompletedProcess(
                command, 0 if self.installed else 1, "", ""
            )
        return subprocess.CompletedProcess(command, 0, "", "")


def test_dependency_install_is_idempotent(tmp_path: Path, monkeypatch: object) -> None:
    product = _product(tmp_path, monkeypatch)
    project = _project(tmp_path)
    _ffmpeg_ready(monkeypatch)
    runner = DependencyRunner(project)
    manager = IngestionManager(product, project_dir=project, command_runner=runner)

    first = manager.install(install_dependencies=True)
    pip_calls_after_first = sum(
        command[1:3] == ["-m", "pip"] for command in runner.commands
    )
    second = manager.install(install_dependencies=True)

    assert first["ready"] is True
    assert second["ready"] is True
    assert pip_calls_after_first == 1
    assert sum(command[1:3] == ["-m", "pip"] for command in runner.commands) == 1


def test_macos_uninstall_disables_only_managed_launchd_jobs(
    tmp_path: Path, monkeypatch: object
) -> None:
    product = _product(tmp_path, monkeypatch)
    project = _project(tmp_path)
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: Any):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("rag_favorite.ingestion.sys.platform", "darwin")
    monkeypatch.setattr(
        "rag_favorite.ingestion.shutil.which", lambda _name: "/bin/tool"
    )
    result = IngestionManager(
        product, project_dir=project, command_runner=runner
    ).uninstall(disable_services=True)

    assert result["steps"] == ["disabled-user-services"]
    bootouts = [
        command for command in commands if command[:2] == ["launchctl", "bootout"]
    ]
    assert len(bootouts) == len(LAUNCHD_LABELS)
    assert {command[-1].rsplit("/", 1)[-1] for command in bootouts} == set(
        LAUNCHD_LABELS
    )


def test_cli_detect_reports_machine_readable_status(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    product = _product(tmp_path, monkeypatch)
    project = _project(tmp_path)
    _ffmpeg_ready(monkeypatch)

    assert (
        main(
            [
                "--config",
                str(product.source),
                "ingestion",
                "detect",
                "--project-dir",
                str(project),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["source_ready"] is True
    assert payload["ready"] is False
    assert payload["project_dir"] == str(project)


class FakePluginOpenClaw:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.fail_probe = False
        self.commands: list[list[str]] = []

    def __call__(
        self, command: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        arguments = command[1:]
        if arguments == ["--version"]:
            return self._result(command, stdout="OpenClaw 1.0\n")
        if arguments == ["mcp", "--help"]:
            return self._result(command, stdout="Configure mcp.servers\n")
        if arguments == ["plugins", "--help"]:
            return self._result(command, stdout="Manage OpenClaw plugins\n")
        if arguments == ["plugins", "enable", PLUGIN_ID]:
            payload, entry = self._ensure_entry()
            entry["enabled"] = True
            self._write(payload)
            return self._result(command)
        if arguments == ["config", "file"]:
            return self._result(command, stdout=str(self.config_path) + "\n")
        if arguments == ["config", "validate", "--json"]:
            return self._result(command, stdout='{"valid":true}\n')
        if arguments[:3] == ["plugins", "inspect", PLUGIN_ID]:
            entry = self._entry()
            if entry is None:
                return self._result(command, returncode=1, stderr="Plugin not found\n")
            source = self._source()
            hooks = entry.get("hooks", {})
            hook_names = ["inbound_claim", "message_received"]
            if hooks.get("allowConversationAccess") is True:
                hook_names.append("before_agent_reply")
            tools = [] if self.fail_probe else [{"names": [CAPABILITY_TOOL]}]
            payload = {
                "plugin": {"id": PLUGIN_ID, "enabled": entry.get("enabled", True)},
                "install": {"sourcePath": source},
                "tools": tools,
                "commands": [
                    "video_status",
                    "video_pause",
                    "video_resume",
                    "video_cancel",
                    "video_batch_status",
                    "video_batch_pause",
                    "video_batch_resume",
                    "video_batch_cancel",
                    "video_login",
                ],
                "typedHooks": [{"name": name} for name in hook_names],
                "diagnostics": [],
            }
            return self._result(command, stdout=json.dumps(payload))
        if arguments == [
            "config",
            "get",
            f"plugins.entries.{PLUGIN_ID}",
            "--json",
        ]:
            entry = self._entry()
            if entry is None:
                return self._result(command, returncode=1)
            return self._result(command, stdout=json.dumps(entry))
        if arguments[:3] == ["plugins", "install", "--link"]:
            payload = self._payload()
            plugins = payload.setdefault("plugins", {})
            plugins.setdefault("load", {})["paths"] = [arguments[3]]
            plugins.setdefault("entries", {})[PLUGIN_ID] = {"enabled": True}
            self._write(payload)
            return self._result(command)
        if arguments[:3] == ["config", "set", f"plugins.entries.{PLUGIN_ID}.config"]:
            payload, entry = self._ensure_entry()
            entry["config"] = json.loads(arguments[3])
            self._write(payload)
            return self._result(command)
        if arguments[:3] == [
            "config",
            "set",
            f"plugins.entries.{PLUGIN_ID}.hooks.allowConversationAccess",
        ]:
            payload, entry = self._ensure_entry()
            entry.setdefault("hooks", {})["allowConversationAccess"] = True
            self._write(payload)
            return self._result(command)
        if arguments == ["plugins", "uninstall", PLUGIN_ID, "--force"]:
            payload = self._payload()
            plugins = payload.setdefault("plugins", {})
            plugins.setdefault("entries", {}).pop(PLUGIN_ID, None)
            plugins.setdefault("load", {})["paths"] = []
            self._write(payload)
            return self._result(command)
        if arguments == ["plugins", "disable", PLUGIN_ID]:
            payload, entry = self._ensure_entry()
            entry["enabled"] = False
            self._write(payload)
            return self._result(command)
        raise AssertionError(f"Unexpected OpenClaw command: {arguments}")

    def _payload(self) -> dict[str, Any]:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def _write(self, payload: dict[str, Any]) -> None:
        self.config_path.write_text(json.dumps(payload), encoding="utf-8")
        self.config_path.chmod(0o600)

    def _entry(self) -> dict[str, Any] | None:
        return self._payload().get("plugins", {}).get("entries", {}).get(PLUGIN_ID)

    def _source(self) -> str:
        paths = self._payload().get("plugins", {}).get("load", {}).get("paths", [])
        return paths[0] if paths else "/unmanaged/plugin"

    def _ensure_entry(self) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = self._payload()
        entry = (
            payload.setdefault("plugins", {})
            .setdefault("entries", {})
            .setdefault(PLUGIN_ID, {"enabled": True})
        )
        return payload, entry

    @staticmethod
    def _result(
        command: list[str],
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def _plugin_manager(tmp_path: Path, monkeypatch: object):
    product = _product(tmp_path, monkeypatch)
    project = _project(tmp_path)
    python = project / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o700)
    executable = tmp_path / "openclaw"
    executable.write_text("test\n", encoding="utf-8")
    executable.chmod(0o700)
    config_path = tmp_path / "openclaw.json"
    config_path.write_text('{"gateway":{"mode":"local"}}\n', encoding="utf-8")
    config_path.chmod(0o600)
    runner = FakePluginOpenClaw(config_path)
    manager = OpenClawPluginManager(
        product,
        project,
        executable=executable,
        openclaw_config=config_path,
        command_runner=runner,
    )
    return manager, runner, config_path


def test_openclaw_plugin_install_is_guarded_probed_and_idempotent(
    tmp_path: Path, monkeypatch: object
) -> None:
    manager, _runner, config_path = _plugin_manager(tmp_path, monkeypatch)

    assert manager.plan()["action"] == "install"
    first = manager.install()
    second = manager.install()
    status = manager.status()

    assert first["action"] == "install"
    assert Path(first["backup"]).stat().st_mode & 0o077 == 0
    assert second["action"] == "none"
    assert status["ready"] is True
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    entry = payload["plugins"]["entries"][PLUGIN_ID]
    assert entry["config"]["projectDir"] == str(manager.project_dir)
    assert entry["hooks"]["allowConversationAccess"] is True

    entry["enabled"] = False
    payload["plugins"]["entries"][PLUGIN_ID] = entry
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    repaired = manager.install()
    assert repaired["action"] == "update"
    assert manager.status()["ready"] is True


def test_openclaw_plugin_probe_failure_restores_exact_config(
    tmp_path: Path, monkeypatch: object
) -> None:
    manager, runner, config_path = _plugin_manager(tmp_path, monkeypatch)
    original = config_path.read_bytes()
    runner.fail_probe = True

    with pytest.raises(OpenClawError, match="incomplete"):
        manager.install()

    assert config_path.read_bytes() == original


def test_openclaw_plugin_refuses_unmanaged_collision(
    tmp_path: Path, monkeypatch: object
) -> None:
    manager, runner, _config_path = _plugin_manager(tmp_path, monkeypatch)
    payload = runner._payload()
    payload["plugins"] = {
        "load": {"paths": ["/other/plugin"]},
        "entries": {PLUGIN_ID: {"enabled": True}},
    }
    runner._write(payload)

    assert manager.plan()["action"] == "conflict"
    with pytest.raises(OpenClawConflictError, match="unmanaged"):
        manager.install()


def test_openclaw_plugin_uninstall_is_targeted(
    tmp_path: Path, monkeypatch: object
) -> None:
    manager, _runner, config_path = _plugin_manager(tmp_path, monkeypatch)
    manager.install()

    result = manager.uninstall()
    payload = json.loads(config_path.read_text(encoding="utf-8"))

    assert result["action"] == "uninstall"
    assert payload["gateway"] == {"mode": "local"}
    assert PLUGIN_ID not in payload["plugins"]["entries"]
    assert result["restart_required"] is True

    restored = manager.restore(Path(result["backup"]))
    assert restored["component"] == PLUGIN_ID
    assert restored["restart_required"] is True
    assert manager.status()["ready"] is True
