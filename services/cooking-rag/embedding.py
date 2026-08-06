"""Compatibility exports for the shared embedding implementation."""

from __future__ import annotations

from product_config import CONFIG
from rag_favorite.embedding import EmbeddingError
from rag_favorite.embedding import OllamaEmbeddingClient as SharedClient


EMBEDDING_MODEL = CONFIG.embedding.model
EMBEDDING_DIMENSIONS = CONFIG.embedding.dimensions
DEFAULT_URL = CONFIG.embedding.url


class OllamaEmbeddingClient(SharedClient):
    def __init__(
        self,
        url: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        settings = CONFIG.embedding
        if url is not None or timeout_seconds is not None:
            settings = type(settings)(
                url=url or settings.url,
                model=settings.model,
                dimensions=settings.dimensions,
                timeout_seconds=timeout_seconds or settings.timeout_seconds,
                batch_size=settings.batch_size,
            )
        super().__init__(settings)
