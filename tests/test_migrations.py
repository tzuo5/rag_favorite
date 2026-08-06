from __future__ import annotations

from pathlib import Path
from typing import Any, Self

import pytest

from rag_favorite.config import load_config
from rag_favorite.migrations import (
    MigrationError,
    MigrationRunner,
    load_migration,
    migration_checksum,
    render_migration,
)


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = rows or []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None


class FakeTransaction:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class FakeConnection:
    def __init__(self, existing: list[tuple[Any, ...]] | None = None) -> None:
        self.existing = existing or []
        self.executed: list[tuple[str, object]] = []
        self.closed = False

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    def execute(self, sql: str, parameters: object = None) -> FakeResult:
        self.executed.append((sql, parameters))
        if "to_regclass" in sql:
            return FakeResult([("rag_favorite_schema_migrations",)])
        if "SELECT version, checksum, embedding_dimensions" in sql:
            return FakeResult(self.existing)
        return FakeResult()

    def close(self) -> None:
        self.closed = True


def _config(tmp_path: Path, dimensions: int = 1024):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""
[embedding]
dimensions = {dimensions}

[[collections]]
key = "notes"
path = "notes"
""".strip(),
        encoding="utf-8",
    )
    return load_config(config_file)


def test_packaged_migration_matches_canonical_checkout() -> None:
    canonical = (
        Path(__file__).parents[1] / "migrations/core/0001_document_rag.sql"
    ).read_text(encoding="utf-8")

    assert load_migration("0001_document_rag.sql") == canonical


def test_migration_runner_applies_once_with_configured_dimensions(
    tmp_path: Path,
) -> None:
    connection = FakeConnection()
    runner = MigrationRunner(_config(tmp_path, 1536), connector=lambda *_a, **_k: connection)

    assert runner.apply() == ("0001_document_rag.sql",)
    assert connection.closed
    sql = "\n".join(statement for statement, _ in connection.executed)
    assert "embedding vector(1536) NOT NULL" in sql
    assert "INSERT INTO rag_favorite_schema_migrations" in sql


def test_migration_runner_preserves_verified_history(tmp_path: Path) -> None:
    source = load_migration("0001_document_rag.sql")
    connection = FakeConnection(
        [("0001_document_rag.sql", migration_checksum(source), 1024)]
    )
    runner = MigrationRunner(_config(tmp_path), connector=lambda *_a, **_k: connection)

    assert runner.apply() == ()
    assert not any(
        "CREATE TABLE IF NOT EXISTS rag_documents" in statement
        for statement, _ in connection.executed
    )


def test_migration_status_handles_fresh_database(tmp_path: Path) -> None:
    connection = FakeConnection()

    def execute_without_ledger(sql: str, parameters: object = None) -> FakeResult:
        connection.executed.append((sql, parameters))
        return FakeResult([(None,)])

    connection.execute = execute_without_ledger  # type: ignore[method-assign]
    runner = MigrationRunner(_config(tmp_path), connector=lambda *_a, **_k: connection)

    assert runner.status() == ()


def test_migration_runner_rejects_changed_history(tmp_path: Path) -> None:
    connection = FakeConnection([("0001_document_rag.sql", "incorrect", 1024)])
    runner = MigrationRunner(_config(tmp_path), connector=lambda *_a, **_k: connection)

    with pytest.raises(MigrationError, match="checksum changed"):
        runner.apply()


def test_render_migration_rejects_invalid_dimensions() -> None:
    with pytest.raises(MigrationError, match="positive"):
        render_migration(load_migration("0001_document_rag.sql"), 0)
