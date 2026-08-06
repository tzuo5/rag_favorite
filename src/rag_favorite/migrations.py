"""Versioned, transactional database migration runner."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from typing import Any

from .config import AppConfig
from .database import connect_database

MIGRATION_PACKAGE = "rag_favorite.resources.migrations"
MIGRATIONS = ("0001_document_rag.sql",)


class MigrationError(RuntimeError):
    """Raised when migration history is unsafe to continue."""


@dataclass(frozen=True, slots=True)
class MigrationRecord:
    version: str
    checksum: str
    embedding_dimensions: int


def load_migration(name: str) -> str:
    if name not in MIGRATIONS:
        raise MigrationError(f"Unknown migration: {name}")
    return resources.files(MIGRATION_PACKAGE).joinpath(name).read_text(encoding="utf-8")


def migration_checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def render_migration(sql: str, dimensions: int) -> str:
    """Render the guarded vector dimension in the initial schema."""

    if dimensions <= 0:
        raise MigrationError("Embedding dimensions must be positive.")
    marker = "vector(1024)"
    if marker not in sql:
        raise MigrationError("Initial migration is missing its vector dimension marker.")
    return sql.replace(marker, f"vector({dimensions})", 1)


class MigrationRunner:
    def __init__(
        self,
        config: AppConfig,
        connector: Callable[..., Any] = connect_database,
    ) -> None:
        self.config = config
        self._connector = connector

    def apply(self) -> tuple[str, ...]:
        connection = self._connector(
            self.config,
            register_pgvector=False,
            application_name="rag-favorite-setup",
        )
        applied: list[str] = []
        try:
            with connection.transaction():
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS rag_favorite_schema_migrations (
                        version text PRIMARY KEY,
                        checksum text NOT NULL,
                        embedding_dimensions integer NOT NULL,
                        applied_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ("rag-favorite-schema-migrations",),
                )
                rows = connection.execute(
                    """
                    SELECT version, checksum, embedding_dimensions
                    FROM rag_favorite_schema_migrations
                    """
                ).fetchall()
                existing = {
                    str(row[0]): MigrationRecord(str(row[0]), str(row[1]), int(row[2]))
                    for row in rows
                }
                for version in MIGRATIONS:
                    source = load_migration(version)
                    checksum = migration_checksum(source)
                    previous = existing.get(version)
                    if previous:
                        if previous.checksum != checksum:
                            raise MigrationError(
                                f"Migration checksum changed after application: {version}"
                            )
                        if previous.embedding_dimensions != self.config.embedding.dimensions:
                            raise MigrationError(
                                "Configured embedding dimensions differ from the installed "
                                f"schema ({previous.embedding_dimensions})."
                            )
                        continue
                    connection.execute(
                        render_migration(source, self.config.embedding.dimensions)
                    )
                    connection.execute(
                        """
                        INSERT INTO rag_favorite_schema_migrations
                            (version, checksum, embedding_dimensions)
                        VALUES (%s, %s, %s)
                        """,
                        (version, checksum, self.config.embedding.dimensions),
                    )
                    applied.append(version)
        finally:
            connection.close()
        return tuple(applied)

    def status(self) -> tuple[MigrationRecord, ...]:
        connection = self._connector(
            self.config,
            register_pgvector=False,
            application_name="rag-favorite-setup-status",
        )
        try:
            relation = connection.execute(
                "SELECT to_regclass('rag_favorite_schema_migrations')"
            ).fetchone()
            if relation is None or relation[0] is None:
                return ()
            rows = connection.execute(
                """
                SELECT version, checksum, embedding_dimensions
                FROM rag_favorite_schema_migrations
                ORDER BY version
                """
            ).fetchall()
            records = tuple(
                MigrationRecord(str(row[0]), str(row[1]), int(row[2]))
                for row in rows
            )
            by_version = {record.version: record for record in records}
            for version in MIGRATIONS:
                record = by_version.get(version)
                if record is None:
                    continue
                expected = migration_checksum(load_migration(version))
                if record.checksum != expected:
                    raise MigrationError(
                        f"Migration checksum changed after application: {version}"
                    )
                if record.embedding_dimensions != self.config.embedding.dimensions:
                    raise MigrationError(
                        "Configured embedding dimensions differ from the installed "
                        f"schema ({record.embedding_dimensions})."
                    )
            return records
        finally:
            connection.close()
