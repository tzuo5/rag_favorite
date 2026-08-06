"""Shared PostgreSQL connection handling."""

from __future__ import annotations

from typing import Any

import psycopg
from pgvector.psycopg import register_vector

from .config import AppConfig, database_credentials, load_config


def connect_database(
    config: AppConfig | None = None,
    *,
    register_pgvector: bool = True,
    application_name: str = "rag-favorite",
) -> psycopg.Connection[Any]:
    resolved = config or load_config()
    settings = database_credentials(resolved)
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
    return connection
