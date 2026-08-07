"""Shared PostgreSQL connection handling."""

from __future__ import annotations

from typing import Any

import psycopg
from pgvector.psycopg import register_vector

from .config import AppConfig, database_credentials, load_config


class DatabaseError(RuntimeError):
    """Stable, user-facing signal for PostgreSQL connection failures."""


def connect_database(
    config: AppConfig | None = None,
    *,
    register_pgvector: bool = True,
    application_name: str = "rag-favorite",
) -> psycopg.Connection[Any]:
    resolved = config or load_config()
    settings = database_credentials(resolved)
    try:
        connection = psycopg.connect(
            host=settings["host"],
            port=int(settings["port"]),
            dbname=settings["name"],
            user=settings["user"],
            password=settings["password"],
            connect_timeout=10,
            application_name=application_name,
            options=(
                "-c statement_timeout=180000 "
                "-c lock_timeout=10000 "
                "-c idle_in_transaction_session_timeout=60000"
            ),
        )
        if register_pgvector:
            register_vector(connection)
    except (psycopg.Error, ValueError) as exc:
        raise DatabaseError(
            f"PostgreSQL is not ready at {settings['host']}:{settings['port']} "
            f"for database {settings['name']!r} and user {settings['user']!r}. "
            "Run `rag-favorite setup status` and follow README.md#complete-rag-setup."
        ) from exc
    return connection
