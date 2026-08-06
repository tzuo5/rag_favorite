"""One validated Ollama embedding client for every product component."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from typing import Protocol

from .config import AppConfig, EmbeddingConfig, load_config

QUERY_INSTRUCTION = (
    "Instruct: Given a user question, retrieve relevant passages "
    "from a private knowledge base.\nQuery: "
)
MAX_RESPONSE_BYTES = 16_000_000


class EmbeddingError(RuntimeError):
    """Stable signal for unavailable or malformed embeddings."""


class EmbeddingProvider(Protocol):
    model: str
    dimensions: int

    def embed_documents(self, texts: list[str]) -> list[tuple[float, ...]]: ...

    def embed_query(self, text: str) -> tuple[float, ...]: ...


class OllamaEmbeddingClient:
    def __init__(
        self,
        settings: EmbeddingConfig | None = None,
        *,
        opener: object = urllib.request,
    ) -> None:
        self.settings = settings or load_config().embedding
        self.model = self.settings.model
        self.dimensions = self.settings.dimensions
        self._opener = opener

    def embed_document(self, text: str) -> tuple[float, ...]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: list[str]) -> list[tuple[float, ...]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._embed([QUERY_INSTRUCTION + text])[0]

    def _embed(self, texts: list[str]) -> list[tuple[float, ...]]:
        if not texts:
            return []
        request = urllib.request.Request(
            self.settings.url,
            data=json.dumps(
                {"model": self.model, "input": texts}, ensure_ascii=False
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.urlopen(
                request, timeout=self.settings.timeout_seconds
            ) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise EmbeddingError("Embedding response is too large.")
            result = json.loads(payload)
        except EmbeddingError:
            raise
        except (
            OSError,
            TimeoutError,
            urllib.error.URLError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise EmbeddingError("Embedding service is unavailable.") from exc

        values = result.get("embeddings")
        if not isinstance(values, list) or len(values) != len(texts):
            raise EmbeddingError("Embedding response count is invalid.")

        embeddings: list[tuple[float, ...]] = []
        for raw_vector in values:
            if not isinstance(raw_vector, list) or len(raw_vector) != self.dimensions:
                raise EmbeddingError("Embedding dimensions are invalid.")
            vector: list[float] = []
            for raw_value in raw_vector:
                if isinstance(raw_value, bool) or not isinstance(
                    raw_value, (int, float)
                ):
                    raise EmbeddingError("Embedding value is invalid.")
                value = float(raw_value)
                if not math.isfinite(value):
                    raise EmbeddingError("Embedding value is invalid.")
                vector.append(value)
            embeddings.append(tuple(vector))
        return embeddings


def client_from_config(config: AppConfig | None = None) -> OllamaEmbeddingClient:
    resolved = config or load_config()
    return OllamaEmbeddingClient(resolved.embedding)
