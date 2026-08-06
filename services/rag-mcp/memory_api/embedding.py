"""Loopback-only Ollama embedding client for governed memory."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from typing import Final, Protocol


OLLAMA_EMBED_URL: Final[str] = "http://127.0.0.1:11434/api/embed"
EMBEDDING_MODEL: Final[str] = "qwen3-embedding:0.6b"
EMBEDDING_DIMENSIONS: Final[int] = 1024
EMBEDDING_TIMEOUT_SECONDS: Final[int] = 120
MAX_RESPONSE_BYTES: Final[int] = 1_000_000

QUERY_INSTRUCTION: Final[str] = (
    "Instruct: Given a user question, retrieve relevant passages "
    "from a personal knowledge base.\n"
    "Query: "
)


class EmbeddingError(RuntimeError):
    """Stable internal signal for unavailable or malformed embeddings."""


class EmbeddingProvider(Protocol):
    model: str
    dimensions: int

    def embed_document(self, text: str) -> tuple[float, ...]:
        ...

    def embed_query(self, text: str) -> tuple[float, ...]:
        ...


class OllamaEmbeddingClient:
    """Request one validated embedding from the loopback Ollama API."""

    model = EMBEDDING_MODEL
    dimensions = EMBEDDING_DIMENSIONS

    def __init__(
        self,
        *,
        url: str = OLLAMA_EMBED_URL,
        timeout_seconds: int = EMBEDDING_TIMEOUT_SECONDS,
        opener: object = urllib.request,
    ) -> None:
        if url != OLLAMA_EMBED_URL:
            raise ValueError("embedding URL must remain loopback-only")

        self._url = url
        self._timeout_seconds = timeout_seconds
        self._opener = opener

    def embed_document(self, text: str) -> tuple[float, ...]:
        return self._embed(text)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._embed(QUERY_INSTRUCTION + text)

    def _embed(self, text: str) -> tuple[float, ...]:
        request_body = json.dumps(
            {
                "model": self.model,
                "input": [text],
            },
            ensure_ascii=False,
        ).encode("utf-8")

        request = urllib.request.Request(
            self._url,
            data=request_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with self._opener.urlopen(
                request,
                timeout=self._timeout_seconds,
            ) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)

            if len(payload) > MAX_RESPONSE_BYTES:
                raise EmbeddingError("embedding response is too large")

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
            raise EmbeddingError("embedding service is unavailable") from exc

        embeddings = result.get("embeddings")

        if not isinstance(embeddings, list) or len(embeddings) != 1:
            raise EmbeddingError("embedding response count is invalid")

        embedding = embeddings[0]

        if (
            not isinstance(embedding, list)
            or len(embedding) != self.dimensions
        ):
            raise EmbeddingError("embedding dimensions are invalid")

        normalized: list[float] = []

        for value in embedding:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise EmbeddingError("embedding value is invalid")

            normalized.append(float(value))

        return tuple(normalized)
