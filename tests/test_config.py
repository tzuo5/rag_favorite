from __future__ import annotations

from pathlib import Path

import pytest

from rag_favorite.config import ConfigError, database_credentials, load_config


def test_custom_collections_and_relative_paths(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    (tmp_path / "secrets.env").write_text(
        "RAG_DATABASE_PASSWORD=test-only\n", encoding="utf-8"
    )
    config_file.write_text(
        """
[database]
credentials_file = "secrets.env"

[embedding]
dimensions = 1024

[[collections]]
key = "research"
name = "Research Notes"
path = "knowledge/research"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert tuple(config.collections) == ("research",)
    assert config.collection("research").path == tmp_path / "knowledge/research"
    assert config.database.credentials_file == tmp_path / "secrets.env"


def test_duplicate_collection_keys_fail_closed(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[[collections]]
key = "notes"
path = "notes"

[[collections]]
key = "notes"
path = "other"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Duplicate collection"):
        load_config(config_file)


def test_non_loopback_services_fail_closed(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[database]
host = "database.example.com"

[[collections]]
key = "notes"
path = "notes"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="loopback"):
        load_config(config_file)


def test_database_credentials_use_owner_only_file(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[database]
credentials_file = "secrets.env"

[[collections]]
key = "notes"
path = "notes"
""".strip(),
        encoding="utf-8",
    )
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("RAG_DATABASE_PASSWORD=test-only\n", encoding="utf-8")
    secrets_file.chmod(0o600)

    credentials = database_credentials(load_config(config_file))

    assert credentials["name"] == "ragdb"
    assert credentials["user"] == "rag_admin"
    assert credentials["password"] == "test-only"


def test_database_credentials_reject_public_file(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[database]
credentials_file = "secrets.env"

[[collections]]
key = "notes"
path = "notes"
""".strip(),
        encoding="utf-8",
    )
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text("RAG_DATABASE_PASSWORD=unsafe\n", encoding="utf-8")
    secrets_file.chmod(0o644)

    with pytest.raises(ConfigError, match="permissions"):
        database_credentials(load_config(config_file))
