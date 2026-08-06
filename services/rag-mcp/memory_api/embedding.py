"""Compatibility exports for the shared embedding client."""

from __future__ import annotations

import urllib.request

from product_config import CONFIG
from rag_favorite.embedding import (
    EmbeddingError,
    EmbeddingProvider,
    OllamaEmbeddingClient as SharedClient,
    QUERY_INSTRUCTION,
)


OLLAMA_EMBED_URL = CONFIG.embedding.url
EMBEDDING_MODEL = CONFIG.embedding.model
EMBEDDING_DIMENSIONS = CONFIG.embedding.dimensions
EMBEDDING_TIMEOUT_SECONDS = CONFIG.embedding.timeout_seconds
MAX_RESPONSE_BYTES = 16_000_000


class OllamaEmbeddingClient(SharedClient):
    def __init__(
        self,
        *,
        url: str = OLLAMA_EMBED_URL,
        timeout_seconds: int = EMBEDDING_TIMEOUT_SECONDS,
        opener: object = urllib.request,
    ) -> None:
        settings = type(CONFIG.embedding)(
            url=url,
            model=CONFIG.embedding.model,
            dimensions=CONFIG.embedding.dimensions,
            timeout_seconds=timeout_seconds,
            batch_size=CONFIG.embedding.batch_size,
        )
        super().__init__(settings, opener=opener)
