from __future__ import annotations

import json
from pathlib import Path

import pytest

from rag_favorite.cli import main
from rag_favorite.config import ConfigError, load_config
from rag_favorite.mcp_support import client_config, write_client_config


def _config(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[[collections]]\nkey = "notes"\nname = "Notes"\npath = "notes"\n',
        encoding="utf-8",
    )
    return load_config(config_file)


def test_client_config_uses_absolute_python_and_contains_no_secret(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    python = tmp_path / "venv/bin/python"

    payload = client_config(config, python_executable=python)

    server = payload["mcpServers"]["rag-favorite"]
    assert server["command"] == str(python.absolute())
    assert server["args"] == ["-m", "rag_favorite.mcp_server"]
    assert server["env"] == {"RAG_FAVORITE_CONFIG": str(config.source)}
    assert "PASSWORD" not in json.dumps(payload)


def test_client_config_write_is_private_and_refuses_overwrite(tmp_path: Path) -> None:
    destination = tmp_path / "client" / "rag-favorite.json"
    payload = client_config(_config(tmp_path))

    assert write_client_config(destination, payload, force=False) == destination
    assert destination.stat().st_mode & 0o077 == 0
    with pytest.raises(ConfigError, match="already exists"):
        write_client_config(destination, payload, force=False)


def test_cli_prints_generic_mcp_fragment(tmp_path: Path, capsys: object) -> None:
    config = _config(tmp_path)

    assert main(["--config", str(config.source), "mcp", "config"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"mcpServers"}
    assert "rag-favorite" in payload["mcpServers"]


def test_cli_mcp_requires_initialized_config(tmp_path: Path, capsys: object) -> None:
    missing = tmp_path / "missing.toml"

    assert main(["--config", str(missing), "mcp", "inspect"]) == 1

    assert "run rag-favorite setup first" in capsys.readouterr().err
