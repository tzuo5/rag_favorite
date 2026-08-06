"""Parameterized repository for governed memory mutation routines."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any, Final

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .database import connect_runtime_database
from .embedding import EmbeddingError, EmbeddingProvider
from .errors import DomainError
from .service import (
    PreparedCreate,
    PreparedLifecycle,
    PreparedUpdate,
)

ConnectionFactory = Callable[[], psycopg.Connection]

_SQLSTATE_ERRORS: Final[dict[str, tuple[str, bool]]] = {
    "P5201": ("VALIDATION_ERROR", False),
    "P5202": ("NOT_FOUND", False),
    "P5203": ("CONCURRENCY_CONFLICT", True),
    "P5204": ("INVALID_STATE", False),
    "P5205": ("ALREADY_ARCHIVED", False),
    "P5206": ("NOT_ARCHIVED", False),
    "P5207": ("IDEMPOTENCY_CONFLICT", False),
    "P5208": ("PERMISSION_DENIED", False),
    "57014": ("TIMEOUT", True),
    "55P03": ("TIMEOUT", True),
}

_CONNECTION_SQLSTATES: Final[frozenset[str]] = frozenset(
    {
        "08000",
        "08001",
        "08003",
        "08004",
        "08006",
        "08007",
        "08P01",
        "57P01",
        "57P02",
        "57P03",
    }
)

_CREATE_SQL: Final[str] = """
SELECT *
FROM public.rag_api_memory_create(
    %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s
)
"""

_UPDATE_SQL: Final[str] = """
SELECT *
FROM public.rag_api_memory_update(
    %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s
)
"""

_ARCHIVE_SQL: Final[str] = """
SELECT *
FROM public.rag_api_memory_archive(
    %s, %s, %s, %s, %s, %s, %s
)
"""

_RESTORE_SQL: Final[str] = """
SELECT *
FROM public.rag_api_memory_restore_ready(
    %s, %s, %s, %s, %s, %s, %s
)
"""

_EMBEDDING_SUCCEED_SQL: Final[str] = """
SELECT *
FROM public.rag_api_memory_embedding_succeed(
    %s, %s, %s, %s, %s, %s, %s::vector,
    %s, %s, %s
)
"""


class MemoryRepository:
    """Execute only the fixed governed-memory database routines."""

    def __init__(
        self,
        connection_factory: ConnectionFactory = connect_runtime_database,
        *,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self._connection_factory = connection_factory
        self._embedding_provider = embedding_provider

    def create(self, prepared: PreparedCreate) -> dict[str, Any]:
        parameters = (
            prepared.memory_id,
            prepared.revision_id,
            prepared.namespace,
            prepared.memory_type,
            prepared.content,
            prepared.content_hash,
            prepared.source_type,
            prepared.source_ref,
            prepared.trust_level,
            prepared.reason,
            prepared.idempotency_key,
            prepared.request_hash,
            Jsonb(prepared.metadata),
        )

        return self._execute_with_embedding(
            _CREATE_SQL,
            parameters,
            prepared,
        )

    def update(self, prepared: PreparedUpdate) -> dict[str, Any]:
        parameters = (
            prepared.memory_id,
            prepared.revision_id,
            prepared.expected_version,
            prepared.expected_state_version,
            prepared.content,
            prepared.content_hash,
            prepared.source_type,
            prepared.source_ref,
            prepared.trust_level,
            prepared.reason,
            prepared.idempotency_key,
            prepared.request_hash,
            Jsonb(prepared.metadata),
        )

        return self._execute_with_embedding(
            _UPDATE_SQL,
            parameters,
            prepared,
        )

    def archive(self, prepared: PreparedLifecycle) -> dict[str, Any]:
        parameters = (
            prepared.memory_id,
            prepared.expected_version,
            prepared.expected_state_version,
            prepared.reason,
            prepared.idempotency_key,
            prepared.request_hash,
            prepared.source_ref,
        )

        return self._execute(_ARCHIVE_SQL, parameters)

    def restore(self, prepared: PreparedLifecycle) -> dict[str, Any]:
        parameters = (
            prepared.memory_id,
            prepared.expected_version,
            prepared.expected_state_version,
            prepared.reason,
            prepared.idempotency_key,
            prepared.request_hash,
            prepared.source_ref,
        )

        return self._execute(_RESTORE_SQL, parameters)

    def _execute(
        self,
        query: str,
        parameters: tuple[object, ...],
    ) -> dict[str, Any]:
        try:
            with self._connection_factory() as connection:
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(query, parameters)
                    row = cursor.fetchone()

                    if row is None:
                        raise DomainError("INTERNAL_ERROR")

                    return dict(row)

        except DomainError:
            raise

        except psycopg.Error as exc:
            raise _map_database_error(exc) from None

        except (OSError, RuntimeError):
            raise DomainError(
                "DATABASE_UNAVAILABLE",
                retryable=True,
            ) from None

    def _execute_with_embedding(
        self,
        query: str,
        parameters: tuple[object, ...],
        prepared: PreparedCreate | PreparedUpdate,
    ) -> dict[str, Any]:
        try:
            with self._connection_factory() as connection:
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(query, parameters)
                    row = cursor.fetchone()

                    if row is None:
                        raise DomainError("INTERNAL_ERROR")

                    result = dict(row)

                    if not bool(result.get("replayed")):
                        provider = self._embedding_provider

                        if provider is None:
                            raise DomainError(
                                "EMBEDDING_UNAVAILABLE",
                                retryable=True,
                            )

                        try:
                            embedding = provider.embed_document(
                                prepared.content
                            )
                        except EmbeddingError:
                            raise DomainError(
                                "EMBEDDING_UNAVAILABLE",
                                retryable=True,
                            ) from None

                        if len(embedding) != provider.dimensions:
                            raise DomainError("INTERNAL_ERROR")

                        embedding_key, embedding_hash = (
                            _embedding_audit_identity(
                                prepared.request_hash,
                                provider.model,
                            )
                        )
                        vector_text = _vector_text(embedding)

                        cursor.execute(
                            _EMBEDDING_SUCCEED_SQL,
                            (
                                prepared.memory_id,
                                prepared.revision_id,
                                result.get("current_version"),
                                result.get("state_version"),
                                provider.model,
                                provider.dimensions,
                                vector_text,
                                embedding_key,
                                embedding_hash,
                                prepared.source_ref,
                            ),
                        )

                        if cursor.fetchone() is None:
                            raise DomainError("INTERNAL_ERROR")

                    return result

        except DomainError:
            raise

        except psycopg.Error as exc:
            raise _map_database_error(exc) from None

        except (OSError, RuntimeError):
            raise DomainError(
                "DATABASE_UNAVAILABLE",
                retryable=True,
            ) from None


def _embedding_audit_identity(
    mutation_request_hash: str,
    model: str,
) -> tuple[str, str]:
    digest = hashlib.sha256(
        (
            "phase5.3-embedding\x00"
            + mutation_request_hash
            + "\x00"
            + model
        ).encode("utf-8")
    ).hexdigest()

    return f"embedding:{digest}", digest


def _vector_text(embedding: tuple[float, ...]) -> str:
    return "[" + ",".join(format(value, ".17g") for value in embedding) + "]"


def _map_database_error(error: psycopg.Error) -> DomainError:
    sqlstate = error.sqlstate

    if sqlstate in _SQLSTATE_ERRORS:
        code, retryable = _SQLSTATE_ERRORS[sqlstate]

        return DomainError(
            code,
            retryable=retryable,
        )

    if sqlstate in _CONNECTION_SQLSTATES:
        return DomainError(
            "DATABASE_UNAVAILABLE",
            retryable=True,
        )

    return DomainError("INTERNAL_ERROR")
