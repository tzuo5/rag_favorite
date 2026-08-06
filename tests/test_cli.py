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
