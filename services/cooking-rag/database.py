from __future__ import annotations

import os
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from pgvector.psycopg import register_vector


DEFAULT_ENV_FILE = Path("/home/ubuntu/services/cooking-rag/runtime.env")


def load_database_settings(env_file: Path | None = None) -> dict[str, str]:
    selected = env_file or Path(
        os.environ.get("COOKING_RAG_ENV_FILE", str(DEFAULT_ENV_FILE))
    )
    required = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")
    selected_exists = selected.is_file()
    raw = dict(dotenv_values(selected)) if selected_exists else {}
    if env_file is None and not selected_exists:
        for name in required:
            if os.environ.get(name):
                raw[name] = os.environ[name]
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
