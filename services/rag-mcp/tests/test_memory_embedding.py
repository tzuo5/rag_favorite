from __future__ import annotations

import json
import unittest

from memory_api.embedding import (
    EMBEDDING_DIMENSIONS,
    EmbeddingError,
    OllamaEmbeddingClient,
    QUERY_INSTRUCTION,
)


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
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
        self.request_body = json.loads(request.data)
        return FakeResponse(self.payload)


class EmbeddingClientTests(unittest.TestCase):
    def test_document_embedding_is_validated(self) -> None:
        opener = FakeOpener(
            {"embeddings": [[1.0] + [0.0] * 1023]}
        )
        client = OllamaEmbeddingClient(opener=opener)

        result = client.embed_document("Stable governed memory.")

        self.assertEqual(len(result), EMBEDDING_DIMENSIONS)
        self.assertEqual(
            opener.request_body["input"],
            ["Stable governed memory."],
        )

    def test_query_instruction_is_applied(self) -> None:
        opener = FakeOpener(
            {"embeddings": [[1.0] + [0.0] * 1023]}
        )
        client = OllamaEmbeddingClient(opener=opener)

        client.embed_query("What did Gordon decide?")

        self.assertEqual(
            opener.request_body["input"],
            [QUERY_INSTRUCTION + "What did Gordon decide?"],
        )

    def test_wrong_dimensions_fail_closed(self) -> None:
        client = OllamaEmbeddingClient(
            opener=FakeOpener({"embeddings": [[1.0, 0.0]]})
        )

        with self.assertRaises(EmbeddingError):
            client.embed_document("Invalid response.")


if __name__ == "__main__":
    unittest.main()
