"""Bounded read-only retrieval for active governed memories."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from .database import connect_runtime_database
from .embedding import EmbeddingError, EmbeddingProvider
from .errors import DomainError
from .repository import ConnectionFactory, _map_database_error, _vector_text


_SEARCH_SQL = """
SELECT *
FROM public.rag_api_memory_search(%s::vector, %s)
"""


class GovernedMemoryRetriever:
    """Embed one query and execute the fixed governed search routine."""

    def __init__(
        self,
        embedding_provider: EmbeddingProvider,
        connection_factory: ConnectionFactory = connect_runtime_database,
    ) -> None:
        self._embedding_provider = embedding_provider
        self._connection_factory = connection_factory

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            embedding = self._embedding_provider.embed_query(query)
        except EmbeddingError:
            raise DomainError(
                "EMBEDDING_UNAVAILABLE",
                retryable=True,
            ) from None

        try:
            with self._connection_factory() as connection:
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(
                        _SEARCH_SQL,
                        (_vector_text(embedding), limit),
                    )
                    rows = cursor.fetchall()

            return [_agent_safe_result(dict(row)) for row in rows]
        except psycopg.Error as exc:
            raise _map_database_error(exc) from None
        except (OSError, RuntimeError):
            raise DomainError(
                "DATABASE_UNAVAILABLE",
                retryable=True,
            ) from None


def _agent_safe_result(row: dict[str, Any]) -> dict[str, Any]:
    memory_id = row.get("memory_id")
    similarity = row.get("cosine_similarity")

    return {
        "memory_id": str(memory_id),
        "namespace": row.get("namespace"),
        "memory_type": row.get("memory_type"),
        "current_version": row.get("current_version"),
        "content": row.get("content"),
        "content_truncated": bool(row.get("content_truncated")),
        "source_type": row.get("source_type"),
        "source_ref": row.get("source_ref"),
        "trust_level": row.get("trust_level"),
        "metadata": row.get("metadata"),
        "cosine_similarity": (
            float(similarity) if similarity is not None else None
        ),
    }
