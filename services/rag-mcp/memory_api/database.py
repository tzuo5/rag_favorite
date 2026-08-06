"""Least-privilege PostgreSQL connection handling for memory tools."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import psycopg
from dotenv import dotenv_values

DEFAULT_RUNTIME_ENV_PATH: Final[Path] = (
    Path.home() / ".openclaw" / "rag-mcp-runtime.env"
)

_ALLOWED_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "127.0.0.1",
        "localhost",
        "::1",
    }
)

_REQUIRED_KEYS: Final[tuple[str, ...]] = (
    "RAG_MCP_DB_HOST",
    "RAG_MCP_DB_PORT",
    "RAG_MCP_DB_NAME",
    "RAG_MCP_DB_USER",
    "RAG_MCP_DB_PASSWORD",
)


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    host: str
    port: int
    database: str
    user: str
    password: str


def load_runtime_database_settings(
    path: Path = DEFAULT_RUNTIME_ENV_PATH,
) -> DatabaseSettings:
    """Load the dedicated MCP runtime credential without exposing values."""

    if not path.is_file():
        raise RuntimeError("Dedicated MCP database credential is missing.")

    file_stat = path.stat()
    mode = stat.S_IMODE(file_stat.st_mode)

    if mode & 0o077:
        raise RuntimeError(
            "Dedicated MCP database credential permissions are too broad."
        )

    if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
        raise RuntimeError(
            "Dedicated MCP database credential has an unexpected owner."
        )

    raw = dotenv_values(path)

    missing = [
        name
        for name in _REQUIRED_KEYS
        if not isinstance(raw.get(name), str)
        or not str(raw[name]).strip()
    ]

    if missing:
        raise RuntimeError(
            "Dedicated MCP database credential is incomplete."
        )

    host = str(raw["RAG_MCP_DB_HOST"]).strip()

    if host not in _ALLOWED_HOSTS:
        raise RuntimeError(
            "Dedicated MCP database host must be loopback-only."
        )

    try:
        port = int(str(raw["RAG_MCP_DB_PORT"]).strip())
    except ValueError as exc:
        raise RuntimeError(
            "Dedicated MCP database port is invalid."
        ) from exc

    if not 1 <= port <= 65535:
        raise RuntimeError(
            "Dedicated MCP database port is invalid."
        )

    return DatabaseSettings(
        host=host,
        port=port,
        database=str(raw["RAG_MCP_DB_NAME"]).strip(),
        user=str(raw["RAG_MCP_DB_USER"]).strip(),
        password=str(raw["RAG_MCP_DB_PASSWORD"]),
    )


def connect_runtime_database(
    settings: DatabaseSettings | None = None,
) -> psycopg.Connection:
    """Open one bounded connection using the dedicated runtime role."""

    resolved = settings or load_runtime_database_settings()

    return psycopg.connect(
        host=resolved.host,
        port=resolved.port,
        dbname=resolved.database,
        user=resolved.user,
        password=resolved.password,
        connect_timeout=5,
        application_name="gordon-rag-mcp",
        options=(
            "-c statement_timeout=10000 "
            "-c lock_timeout=3000 "
            "-c idle_in_transaction_session_timeout=10000"
        ),
    )
