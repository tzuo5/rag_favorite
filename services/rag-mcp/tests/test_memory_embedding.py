from __future__ import annotations

import json
import unittest
from dataclasses import replace
from typing import Self

from rag_favorite.config import EmbeddingConfig

PINNED = replace(
    EmbeddingConfig(),
    backend="ollama",
    url="http://127.0.0.1:11434/api/embed",
    model="qwen3-embedding:0.6b",
    model_digest="a" * 64,
)

from memory_api.embedding import (
    EMBEDDING_DIMENSIONS,
    QUERY_INSTRUCTION,
    EmbeddingError,
    OllamaEmbeddingClient,
)


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self.payload[:size]


class FakeOpener:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.request_body: dict[str, object] | None = None

    def urlopen(self, request: object, timeout: int) -> FakeResponse:
        if request.data is None:
            return FakeResponse(
                {"models": [{"name": PINNED.model, "digest": PINNED.model_digest}]}
            )
        self.request_body = json.loads(request.data)
        return FakeResponse({"model": PINNED.model, **self.payload})


class EmbeddingClientTests(unittest.TestCase):
    def test_document_embedding_is_validated(self) -> None:
        opener = FakeOpener({"embeddings": [[1.0] + [0.0] * 1023]})
        client = OllamaEmbeddingClient(opener=opener, settings=PINNED)

        result = client.embed_document("Stable governed memory.")

        self.assertEqual(len(result), EMBEDDING_DIMENSIONS)
        self.assertEqual(
            opener.request_body["input"],
            ["Stable governed memory."],
        )

    def test_query_instruction_is_applied(self) -> None:
        opener = FakeOpener({"embeddings": [[1.0] + [0.0] * 1023]})
        client = OllamaEmbeddingClient(opener=opener, settings=PINNED)

        client.embed_query("What did Gordon decide?")

        self.assertEqual(
            opener.request_body["input"],
            [QUERY_INSTRUCTION + "What did Gordon decide?"],
        )

    def test_wrong_dimensions_fail_closed(self) -> None:
        client = OllamaEmbeddingClient(
            opener=FakeOpener({"embeddings": [[1.0, 0.0]]}), settings=PINNED
        )

        with self.assertRaises(EmbeddingError):
            client.embed_document("Invalid response.")

    def test_overriding_transport_preserves_the_encoder_identity(self):
        client = OllamaEmbeddingClient(
            url="http://127.0.0.1:11435/api/embed", settings=PINNED
        )
        self.assertEqual(client.settings.model_digest, PINNED.model_digest)
        self.assertEqual(client.settings.query_instruction, PINNED.query_instruction)


if __name__ == "__main__":
    unittest.main()
