from __future__ import annotations

import json
import plistlib
import subprocess
from pathlib import Path

import pytest

from rag_favorite.cli import main
from rag_favorite.config import default_config
from rag_favorite.embedding import EmbeddingError
from rag_favorite.migrations import (
    MigrationRecord,
    load_migration,
    migration_checksum,
)
from rag_favorite.setup import LAUNCHD_LABELS, SYSTEMD_UNITS, SetupManager, SetupOptions


def _xdg_environment(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data-home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))


def test_setup_plan_is_read_only(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    _xdg_environment(monkeypatch, tmp_path)
    config_file = tmp_path / "profile" / "config.toml"

    assert main(["--config", str(config_file), "setup", "plan", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"][0]["status"] == "create"
    assert not config_file.exists()


def test_setup_apply_without_dependencies_is_idempotent(
    tmp_path: Path, monkeypatch: object
) -> None:
    _xdg_environment(monkeypatch, tmp_path)
    config_file = tmp_path / "profile" / "config.toml"
    command = [
        "--config",
        str(config_file),
        "setup",
        "apply",
        "--skip-database",
    ]

    assert main(command) == 0
    original = config_file.read_text(encoding="utf-8")
    assert main(command) == 0

    assert config_file.read_text(encoding="utf-8") == original
    assert config_file.stat().st_mode & 0o077 == 0
    assert (tmp_path / "profile" / "secrets.env").stat().st_mode & 0o077 == 0
    state = tmp_path / "state-home" / "rag-favorite" / "setup.json"
    assert state.is_file()
    assert state.stat().st_mode & 0o077 == 0


def test_setup_status_reports_missing_config(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    _xdg_environment(monkeypatch, tmp_path)
    config_file = tmp_path / "missing.toml"

    assert main(["--config", str(config_file), "setup", "status", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"][0]["status"] == "missing"


def test_setup_renders_private_user_systemd_units(
    tmp_path: Path, monkeypatch: object
) -> None:
    _xdg_environment(monkeypatch, tmp_path)
    config_file = tmp_path / "profile" / "config.toml"
    ingestion_dir = tmp_path / "ingestion"
    (ingestion_dir / "backend").mkdir(parents=True)
    (ingestion_dir / ".env").write_text("TEST_ONLY=1\n", encoding="utf-8")
    systemd_dir = tmp_path / "units"
    manager = SetupManager(config_file)

    results = manager.apply(
        SetupOptions(
            migrate_database=False,
            render_systemd=True,
            ingestion_dir=ingestion_dir,
            systemd_dir=systemd_dir,
        )
    )

    assert results[-1].status == "rendered"
    assert {path.name for path in systemd_dir.iterdir()} == set(SYSTEMD_UNITS)
    worker = (systemd_dir / "video-ingestion-worker.service").read_text(
        encoding="utf-8"
    )
    assert "@" not in worker
    assert str(ingestion_dir) in worker
    assert (systemd_dir / "video-ingestion-worker.service").stat().st_mode & 0o077 == 0


def test_setup_renders_private_launchd_jobs_with_space_safe_paths(
    tmp_path: Path, monkeypatch: object
) -> None:
    _xdg_environment(monkeypatch, tmp_path)
    config_file = tmp_path / "profile" / "config.toml"
    ingestion_dir = tmp_path / "Application Support" / "ingestion"
    (ingestion_dir / "backend").mkdir(parents=True)
    (ingestion_dir / ".env").write_text("TEST_ONLY=1\n", encoding="utf-8")
    launchd_dir = tmp_path / "Launch Agents"

    results = SetupManager(config_file).apply(
        SetupOptions(
            migrate_database=False,
            render_launchd=True,
            ingestion_dir=ingestion_dir,
            launchd_dir=launchd_dir,
        )
    )

    assert results[-1].name == "user-launchd"
    assert results[-1].status == "rendered"
    assert {path.stem for path in launchd_dir.glob("*.plist")} == set(LAUNCHD_LABELS)
    worker_path = launchd_dir / "com.rag-favorite.video-ingestion-worker.plist"
    worker = plistlib.loads(worker_path.read_bytes())
    assert worker["Label"] == "com.rag-favorite.video-ingestion-worker"
    assert worker["RunAtLoad"] is True
    assert worker["KeepAlive"] == {"SuccessfulExit": False}
    assert worker["WorkingDirectory"] == str(ingestion_dir)
    assert worker_path.stat().st_mode & 0o077 == 0
    wrapper = Path(worker["ProgramArguments"][0])
    assert wrapper.stat().st_mode & 0o077 == 0
    assert "'" + str(ingestion_dir / ".env") + "'" in wrapper.read_text(
        encoding="utf-8"
    )


def test_setup_enables_launchd_jobs_through_gui_domain(
    tmp_path: Path, monkeypatch: object
) -> None:
    _xdg_environment(monkeypatch, tmp_path)
    monkeypatch.setattr("rag_favorite.setup.shutil.which", lambda name: f"/bin/{name}")
    config_file = tmp_path / "profile" / "config.toml"
    ingestion_dir = tmp_path / "ingestion"
    (ingestion_dir / "backend").mkdir(parents=True)
    (ingestion_dir / ".venv/bin").mkdir(parents=True)
    (ingestion_dir / ".venv/bin/python").write_text("", encoding="utf-8")
    (ingestion_dir / ".env").write_text("TEST_ONLY=1\n", encoding="utf-8")
    launchd_dir = tmp_path / "launchd"
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    results = SetupManager(config_file, command_runner=runner).apply(
        SetupOptions(
            migrate_database=False,
            render_launchd=True,
            enable_services=True,
            ingestion_dir=ingestion_dir,
            launchd_dir=launchd_dir,
        )
    )

    assert results[-1].status == "enabled"
    assert sum(command[1] == "bootout" for command in commands) == len(LAUNCHD_LABELS)
    assert sum(command[1] == "bootstrap" for command in commands) == len(LAUNCHD_LABELS)
    assert all(command[2].startswith("gui/") for command in commands)


def test_launchd_enable_failure_removes_jobs_started_in_this_attempt(
    tmp_path: Path, monkeypatch: object
) -> None:
    _xdg_environment(monkeypatch, tmp_path)
    monkeypatch.setattr("rag_favorite.setup.shutil.which", lambda _name: "/bin/tool")
    config_file = tmp_path / "profile/config.toml"
    ingestion_dir = tmp_path / "ingestion"
    (ingestion_dir / "backend").mkdir(parents=True)
    (ingestion_dir / ".venv/bin").mkdir(parents=True)
    (ingestion_dir / ".venv/bin/python").write_text("", encoding="utf-8")
    (ingestion_dir / ".env").write_text("TEST_ONLY=1\n", encoding="utf-8")
    commands: list[list[str]] = []
    bootstraps = 0

    def runner(command: list[str], **_kwargs: object):
        nonlocal bootstraps
        commands.append(command)
        if command[1] == "bootstrap":
            bootstraps += 1
            if bootstraps == 2:
                return subprocess.CompletedProcess(command, 1, "", "failed")
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(RuntimeError, match="failed"):
        SetupManager(config_file, command_runner=runner).apply(
            SetupOptions(
                migrate_database=False,
                render_launchd=True,
                enable_services=True,
                ingestion_dir=ingestion_dir,
                launchd_dir=tmp_path / "launchd",
            )
        )

    first_label = LAUNCHD_LABELS[0]
    assert sum(command[-1].endswith(first_label) for command in commands) == 2


def test_setup_status_detects_missing_embedding_model(
    tmp_path: Path, monkeypatch: object
) -> None:
    _xdg_environment(monkeypatch, tmp_path)
    config_file = tmp_path / "profile" / "config.toml"
    assert (
        main(["--config", str(config_file), "setup", "apply", "--skip-database"]) == 0
    )
    migration = load_migration("0001_document_rag.sql")

    class CurrentMigrations:
        def __init__(self, _config: object) -> None:
            pass

        def status(self) -> tuple[MigrationRecord, ...]:
            return (
                MigrationRecord(
                    "0001_document_rag.sql", migration_checksum(migration), 1024
                ),
            )

    def unavailable(_self):
        raise EmbeddingError(
            "Configured LM Studio embedding model is not loaded uniquely."
        )

    monkeypatch.setattr(
        "rag_favorite.setup.LMStudioEmbeddingClient.inspect_model",
        unavailable,
    )
    manager = SetupManager(
        config_file,
        migration_runner_factory=CurrentMigrations,
    )

    checks = manager.status()

    assert next(check for check in checks if check.name == "embedding").status == (
        "model-missing"
    )


def test_lmstudio_setup_starts_only_database_and_rejects_ollama_pull(
    tmp_path, monkeypatch
):
    _xdg_environment(monkeypatch, tmp_path)
    manager = SetupManager(tmp_path / "config.toml")
    config = default_config()
    calls = []
    monkeypatch.setattr("rag_favorite.setup.shutil.which", lambda _name: "/bin/docker")
    monkeypatch.setattr(manager, "_compose", lambda _config, *args: calls.append(args))
    manager._start_services(config)
    assert calls == [("up", "-d", "postgres")]
    with pytest.raises(RuntimeError, match="only for Ollama"):
        manager.plan(SetupOptions(start_services=True, pull_model=True))
