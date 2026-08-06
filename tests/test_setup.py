from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Self

from rag_favorite.cli import main
from rag_favorite.migrations import (
    MigrationRecord,
    load_migration,
    migration_checksum,
)
from rag_favorite.setup import SYSTEMD_UNITS, SetupManager, SetupOptions


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


def test_setup_status_detects_missing_embedding_model(
    tmp_path: Path, monkeypatch: object
) -> None:
    _xdg_environment(monkeypatch, tmp_path)
    config_file = tmp_path / "profile" / "config.toml"
    assert main(
        ["--config", str(config_file), "setup", "apply", "--skip-database"]
    ) == 0
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

    class Response(BytesIO):
        status = 200

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        "rag_favorite.setup.urllib.request.urlopen",
        lambda *_args, **_kwargs: Response(b'{"models": []}'),
    )
    manager = SetupManager(
        config_file,
        migration_runner_factory=CurrentMigrations,
    )

    checks = manager.status()

    assert next(check for check in checks if check.name == "embedding").status == (
        "model-missing"
    )
