"""Compatibility exports for the shared embedding client."""

from __future__ import annotations

from dataclasses import replace

from product_config import CONFIG

from rag_favorite.config import EmbeddingConfig
from rag_favorite.embedding import QUERY_INSTRUCTION, EmbeddingError, EmbeddingProvider
from rag_favorite.embedding import OllamaEmbeddingClient as SharedClient

__all__ = [
    "EMBEDDING_DIMENSIONS",
    "EMBEDDING_MODEL",
    "EMBEDDING_TIMEOUT_SECONDS",
    "MAX_RESPONSE_BYTES",
    "OLLAMA_EMBED_URL",
    "QUERY_INSTRUCTION",
    "EmbeddingError",
    "EmbeddingProvider",
    "OllamaEmbeddingClient",
]

OLLAMA_EMBED_URL = CONFIG.embedding.url
EMBEDDING_MODEL = CONFIG.embedding.model
EMBEDDING_DIMENSIONS = CONFIG.embedding.dimensions
EMBEDDING_TIMEOUT_SECONDS = CONFIG.embedding.timeout_seconds
MAX_RESPONSE_BYTES = 16_000_000


class OllamaEmbeddingClient(SharedClient):
    def __init__(
        self,
        *,
        url: str | None = None,
        timeout_seconds: int | None = None,
        opener: object | None = None,
        settings: EmbeddingConfig | None = None,
    ) -> None:
        selected = settings or CONFIG.embedding
        if selected.backend != "ollama":
            raise EmbeddingError(
                "Legacy memory embeddings require an explicit local Ollama configuration."
            )
        settings = replace(
            selected,
            url=url or selected.url,
            timeout_seconds=timeout_seconds or selected.timeout_seconds,
        )
        super().__init__(settings, opener=opener)
