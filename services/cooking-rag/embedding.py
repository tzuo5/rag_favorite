"""Compatibility exports for the shared embedding implementation."""

from __future__ import annotations

from dataclasses import replace

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
        if settings.backend != "ollama":
            raise EmbeddingError(
                "Legacy recipe tables require the original Ollama space. Use a versioned cooking collection for LM Studio or another provider."
            )
        if url is not None or timeout_seconds is not None:
            settings = replace(
                settings,
                url=url or settings.url,
                timeout_seconds=timeout_seconds or settings.timeout_seconds,
            )
        super().__init__(settings)
