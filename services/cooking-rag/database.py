from __future__ import annotations

import os
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

from product_config import CONFIG
from rag_favorite.config import read_env_file

DEFAULT_ENV_FILE = CONFIG.database.credentials_file


def load_database_settings(env_file: Path | None = None) -> dict[str, str]:
    selected = env_file or Path(
        os.environ.get("COOKING_RAG_ENV_FILE", str(DEFAULT_ENV_FILE))
    )
    required = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")
    selected_exists = selected.is_file()
    raw = read_env_file(selected) if selected_exists else {}
    if env_file is None and not selected_exists:
        for name in required:
            if os.environ.get(name):
                raw[name] = os.environ[name]
    defaults = {
        "PGHOST": CONFIG.database.host,
        "PGPORT": str(CONFIG.database.port),
        "PGDATABASE": CONFIG.database.name,
        "PGUSER": CONFIG.database.user,
        "PGPASSWORD": (
            raw.get("RAG_DATABASE_PASSWORD")
            or raw.get("POSTGRES_PASSWORD")
        ),
    }
    aliases = {
        "PGDATABASE": raw.get("RAG_DATABASE_NAME") or raw.get("POSTGRES_DB"),
        "PGUSER": raw.get("RAG_DATABASE_USER") or raw.get("POSTGRES_USER"),
    }
    for name in required:
        raw[name] = raw.get(name) or aliases.get(name) or defaults.get(name)
    missing = [name for name in required if not raw.get(name)]
    if missing:
        raise RuntimeError("cooking RAG database settings are incomplete")

    settings = {name: str(raw[name]) for name in required}
    if settings["PGHOST"] not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("cooking RAG database must remain loopback-only")
    return settings


def connect_database(env_file: Path | None = None) -> psycopg.Connection:
    settings = load_database_settings(env_file)
    connection = psycopg.connect(
        host=settings["PGHOST"],
        port=int(settings["PGPORT"]),
        dbname=settings["PGDATABASE"],
        user=settings["PGUSER"],
        password=settings["PGPASSWORD"],
        connect_timeout=10,
    )
    register_vector(connection)
    return connection
