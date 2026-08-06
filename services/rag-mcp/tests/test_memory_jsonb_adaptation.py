from __future__ import annotations

import unittest
from typing import Any

from psycopg.types.json import Jsonb

from memory_api.contracts import (
    CreateMemoryRequest,
    UpdateMemoryRequest,
)
from memory_api.repository import MemoryRepository
from memory_api.service import prepare_create, prepare_update


class FakeEmbeddingProvider:
    model = "qwen3-embedding:0.6b"
    dimensions = 1024

    def embed_document(self, text: str) -> tuple[float, ...]:
        return (1.0,) + (0.0,) * 1023

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self.embed_document(text)


class FakeCursor:
    def __init__(self) -> None:
        self.query: str | None = None
        self.parameters: tuple[object, ...] | None = None
        self.parameter_sets: list[tuple[object, ...]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(
        self,
        query: str,
        parameters: tuple[object, ...],
    ) -> None:
        self.query = query
        self.parameters = parameters
        self.parameter_sets.append(parameters)

    def fetchone(self) -> dict[str, Any]:
        return {"operation": "test"}


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self.fake_cursor = cursor

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self, **kwargs: object) -> FakeCursor:
        return self.fake_cursor


class JsonbAdaptationTests(unittest.TestCase):
    def test_create_metadata_uses_jsonb_adapter(self) -> None:
        cursor = FakeCursor()
        repository = MemoryRepository(
            connection_factory=lambda: FakeConnection(cursor),
            embedding_provider=FakeEmbeddingProvider(),
        )

        request = CreateMemoryRequest(
            namespace="project_memory",
            memory_type="decision",
            content="Use governed writes.",
            source_type="user_explicit",
            trust_level="high",
            reason="Test JSONB adaptation.",
            idempotency_key="phase52.jsonb.create1",
            metadata={"project": "gordon-core-tokyo"},
        )

        repository.create(prepare_create(request))

        self.assertIsInstance(cursor.parameter_sets[0][-1], Jsonb)

    def test_update_metadata_uses_jsonb_adapter(self) -> None:
        cursor = FakeCursor()
        repository = MemoryRepository(
            connection_factory=lambda: FakeConnection(cursor),
            embedding_provider=FakeEmbeddingProvider(),
        )

        request = UpdateMemoryRequest(
            memory_id="11111111-1111-4111-8111-111111111111",
            expected_version=1,
            expected_state_version=1,
            content="Updated governed content.",
            source_type="user_explicit",
            trust_level="high",
            reason="Test JSONB adaptation.",
            idempotency_key="phase52.jsonb.update1",
            metadata={"language": "en"},
        )

        repository.update(prepare_update(request))

        self.assertIsInstance(cursor.parameter_sets[0][-1], Jsonb)


if __name__ == "__main__":
    unittest.main()
