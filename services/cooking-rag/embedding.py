from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from typing import Final


EMBEDDING_MODEL: Final[str] = "qwen3-embedding:0.6b"
EMBEDDING_DIMENSIONS: Final[int] = 1024
DEFAULT_URL: Final[str] = "http://127.0.0.1:11434/api/embed"
QUERY_PREFIX: Final[str] = (
    "Instruct: Retrieve the most relevant private cooking recipe, ingredient, "
    "technique, drink, or menu-planning passage.\nQuery: "
)


class EmbeddingError(RuntimeError):
    pass


class OllamaEmbeddingClient:
    model = EMBEDDING_MODEL
    dimensions = EMBEDDING_DIMENSIONS

    def __init__(self, url: str = DEFAULT_URL, timeout_seconds: int = 120) -> None:
        if not url.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError("embedding URL must remain loopback-only")
        self.url = url
        self.timeout_seconds = timeout_seconds

    def embed_documents(self, texts: list[str]) -> list[tuple[float, ...]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._embed([QUERY_PREFIX + text])[0]

    def _embed(self, texts: list[str]) -> list[tuple[float, ...]]:
        body = json.dumps(
            {"model": EMBEDDING_MODEL, "input": texts},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                if response.status != 200:
                    raise EmbeddingError("embedding service returned an error")
                payload = json.load(response)
        except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
            raise EmbeddingError("embedding service is unavailable") from exc

        values = payload.get("embeddings")
        if not isinstance(values, list) or len(values) != len(texts):
            raise EmbeddingError("embedding response count is invalid")

        result: list[tuple[float, ...]] = []
        for vector in values:
            if not isinstance(vector, list) or len(vector) != EMBEDDING_DIMENSIONS:
                raise EmbeddingError("embedding dimensions are invalid")
            converted = tuple(float(value) for value in vector)
            if not all(math.isfinite(value) for value in converted):
                raise EmbeddingError("embedding contains a non-finite value")
            result.append(converted)
        return result
