"""Shared PostgreSQL connection handling."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from threading import Lock
from typing import Any

import psycopg
from pgvector.psycopg import register_vector
from psycopg.adapt import AdaptersMap
from psycopg.types import TypeInfo

from .config import AppConfig, database_credentials, load_config

_VECTOR_TYPES = ("vector", "bit", "halfvec", "sparsevec")
_Identity = tuple[str, int, str, str, str]
_Signature = tuple[tuple[int, int] | None, ...]


@dataclass(frozen=True)
class _VectorTemplate:
    signature: _Signature
    adapters: AdaptersMap


# No credentials or live connections are retained. Replacing an entry discards
# the previous template for that identity, including obsolete type OIDs.
_vector_templates: dict[_Identity, _VectorTemplate] = {}
_vector_template_lock = Lock()


def _vector_signature(connection: psycopg.Connection[Any]) -> _Signature:
    # TypeInfo.fetch uses a transaction internally and leaves an idle connection
    # idle. Check the server, not the inherited adapters' potentially stale OIDs.
    infos = (TypeInfo.fetch(connection, name) for name in _VECTOR_TYPES)
    return tuple((info.oid, info.array_oid) if info else None for info in infos)


def _registered_signature(adapters: AdaptersMap) -> _Signature:
    return tuple(
        (info.oid, info.array_oid) if (info := adapters.types.get(name)) else None
        for name in _VECTOR_TYPES
    )


def connect_database(
    config: AppConfig | None = None,
    *,
    register_pgvector: bool = True,
    application_name: str = "rag-favorite",
) -> psycopg.Connection[Any]:
    resolved = config or load_config()
    settings = database_credentials(resolved)
    search_path = f"{resolved.index.schema},public"
    parameters = {
        "host": settings["host"],
        "port": int(settings["port"]),
        "dbname": settings["name"],
        "user": settings["user"],
        "password": settings["password"],
        "connect_timeout": 10,
        "application_name": application_name,
        "options": (
            "-c statement_timeout=180000 "
            "-c lock_timeout=10000 "
            "-c idle_in_transaction_session_timeout=60000 "
            f"-c search_path={search_path}"
        ),
    }
    if not register_pgvector:
        return psycopg.connect(**parameters)

    identity = (
        settings["host"],
        int(settings["port"]),
        settings["name"],
        settings["user"],
        search_path,
    )
    while True:
        with _vector_template_lock:
            template = _vector_templates.get(identity)
        connection = psycopg.connect(
            **parameters, context=template.adapters if template else None
        )
        try:
            signature = _vector_signature(connection)
            if template is not None and template.signature == signature:
                return connection
            # pgvector creates new dumper classes on every registration. Lock
            # the cold path so concurrent first connections register only once.
            with _vector_template_lock:
                current = _vector_templates.get(identity)
                if current is None or current.signature != signature:
                    if template is not None:
                        # Reconnect with a clean public context: no obsolete OID
                        # loaders from a dropped/recreated extension can survive.
                        connection.close()
                        connection = psycopg.connect(**parameters)
                    register_vector(connection)
                    adapters = AdaptersMap(connection.adapters)
                    _vector_templates[identity] = _VectorTemplate(
                        _registered_signature(adapters), adapters
                    )
                    return connection
            # Another thread initialized the same identity while we connected.
            # Inherit its template through Psycopg's public context interface.
            connection.close()
        except BaseException:
            # Cleanup errors must not replace the original initialization error.
            with suppress(Exception):
                connection.close()
            raise
