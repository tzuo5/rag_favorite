from __future__ import annotations

import unittest
from uuid import UUID

from memory_api.retrieval import GovernedMemoryRetriever


class FakeEmbeddingProvider:
    model = "qwen3-embedding:0.6b"
    dimensions = 1024

    def embed_document(self, text: str) -> tuple[float, ...]:
        return (1.0,) + (0.0,) * 1023

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self.embed_document(text)


class FakeCursor:
    def __init__(self) -> None:
        self.query = ""
        self.parameters: tuple[object, ...] = ()

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str, parameters: tuple[object, ...]) -> None:
        self.query = query
        self.parameters = parameters

    def fetchall(self) -> list[dict[str, object]]:
        return [
            {
                "memory_id": UUID(
                    "11111111-1111-4111-8111-111111111111"
                ),
                "namespace": "project_memory",
                "memory_type": "decision",
                "current_version": 2,
                "content": "Use the governed recall path.",
                "content_truncated": False,
                "source_type": "user_explicit",
                "source_ref": "telegram:test",
                "trust_level": "high",
                "metadata": {"project": "gordon-core-tokyo"},
                "cosine_similarity": 0.91,
            }
        ]


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self.fake_cursor = cursor

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self, **kwargs: object) -> FakeCursor:
        return self.fake_cursor


class RetrievalTests(unittest.TestCase):
    def test_fixed_bounded_search_and_safe_result(self) -> None:
        cursor = FakeCursor()
        retriever = GovernedMemoryRetriever(
            FakeEmbeddingProvider(),
            connection_factory=lambda: FakeConnection(cursor),
        )

        results = retriever.search("What was decided?", 3)

        self.assertIn("public.rag_api_memory_search", cursor.query)
        self.assertEqual(cursor.parameters[1], 3)
        self.assertEqual(
            results[0]["memory_id"],
            "11111111-1111-4111-8111-111111111111",
        )
        self.assertEqual(results[0]["cosine_similarity"], 0.91)
        self.assertFalse(results[0]["content_truncated"])


if __name__ == "__main__":
    unittest.main()
