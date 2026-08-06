from __future__ import annotations

from pathlib import Path

from rag_favorite.cli import main


def test_config_init_is_idempotent_and_private(
    tmp_path: Path, capsys: object, monkeypatch: object
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_file = tmp_path / "config.toml"

    assert main(["--config", str(config_file), "config", "init"]) == 0
    assert config_file.is_file()
    assert (tmp_path / "secrets.env").stat().st_mode & 0o077 == 0
    assert main(["--config", str(config_file), "config", "validate"]) == 0


def test_collection_list_uses_custom_config(tmp_path: Path, capsys: object) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[[collections]]
key = "research"
name = "Research"
path = "research"
""".strip(),
        encoding="utf-8",
    )

    assert main(["--config", str(config_file), "collection", "list", "--json"]) == 0
    output = capsys.readouterr().out
    assert '"key": "research"' in output


def test_invalid_config_returns_safe_cli_error(tmp_path: Path, capsys: object) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("not valid toml = [", encoding="utf-8")

    assert main(["--config", str(config_file), "setup", "status"]) == 1

    assert "Unable to load configuration" in capsys.readouterr().err


def test_config_force_recovers_invalid_file(
    tmp_path: Path, monkeypatch: object
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config_file = tmp_path / "config.toml"
    config_file.write_text("not valid toml = [", encoding="utf-8")

    assert main(["--config", str(config_file), "config", "init", "--force"]) == 0

    assert "[[collections]]" in config_file.read_text(encoding="utf-8")
