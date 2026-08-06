from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from memory_api.contracts import (
    ArchiveMemoryRequest,
    CreateMemoryRequest,
    RestoreMemoryRequest,
    UpdateMemoryRequest,
)
from memory_api.database import load_runtime_database_settings
from memory_api.errors import DomainError
from memory_api.repository import (
    MemoryRepository,
    _map_database_error,
)
from memory_api.service import (
    prepare_archive,
    prepare_create,
    prepare_restore,
    prepare_update,
)


class FakeEmbeddingProvider:
    model = "qwen3-embedding:0.6b"
    dimensions = 1024

    def embed_document(self, text: str) -> tuple[float, ...]:
        return (1.0,) + (0.0,) * 1023

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self.embed_document(text)


class FakeDatabaseError(Exception):
    def __init__(self, sqlstate: str | None) -> None:
        super().__init__("database detail that must not escape")
        self.sqlstate = sqlstate


class FakeCursor:
    def __init__(
        self,
        row: dict[str, object] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.row = row
        self.error = error
        self.executed_query: str | None = None
        self.executed_parameters: tuple[object, ...] | None = None
        self.executed_queries: list[str] = []
        self.executed_parameter_sets: list[tuple[object, ...]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(
        self,
        query: str,
        parameters: tuple[object, ...],
    ) -> None:
        self.executed_query = query
        self.executed_parameters = parameters
        self.executed_queries.append(query)
        self.executed_parameter_sets.append(parameters)

        if self.error is not None:
            raise self.error

    def fetchone(self) -> dict[str, object] | None:
        return self.row


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self.fake_cursor = cursor

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self, **_: object) -> FakeCursor:
        return self.fake_cursor


class RepositoryTests(unittest.TestCase):
    def _success_row(self, operation: str) -> dict[str, object]:
        return {
            "operation": operation,
            "memory_id": UUID(
                "11111111-1111-4111-8111-111111111111"
            ),
            "revision_id": UUID(
                "22222222-2222-4222-8222-222222222222"
            ),
            "event_id": 1,
            "current_version": 1,
            "state_version": 1,
            "status": "active",
            "content_hash": "a" * 64,
            "replayed": False,
        }

    def test_create_uses_fixed_parameterized_routine(self) -> None:
        cursor = FakeCursor(self._success_row("create"))
        repository = MemoryRepository(
            connection_factory=lambda: FakeConnection(cursor),
            embedding_provider=FakeEmbeddingProvider(),
        )

        request = CreateMemoryRequest(
            namespace="project_memory",
            memory_type="decision",
            content="Use governed mutation routines.",
            source_type="user_explicit",
            trust_level="high",
            reason="Explicit implementation decision.",
            idempotency_key="phase52.repository.create1",
            metadata={"project": "gordon-core-tokyo"},
        )

        prepared = prepare_create(request)
        result = repository.create(prepared)

        self.assertEqual(result["operation"], "create")
        self.assertIn(
            "public.rag_api_memory_create",
            cursor.executed_queries[0],
        )
        self.assertEqual(
            cursor.executed_queries[0].count("%s"),
            13,
        )
        self.assertEqual(
            len(cursor.executed_parameter_sets[0]),
            13,
        )
        self.assertNotIn(
            prepared.content,
            cursor.executed_queries[0],
        )
        self.assertIn(
            "public.rag_api_memory_embedding_succeed",
            cursor.executed_queries[1],
        )

    def test_update_uses_fixed_parameterized_routine(self) -> None:
        cursor = FakeCursor(self._success_row("update"))
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
            reason="Correct prior content.",
            idempotency_key="phase52.repository.update1",
        )

        prepared = prepare_update(request)
        repository.update(prepared)

        self.assertIn(
            "public.rag_api_memory_update",
            cursor.executed_queries[0],
        )
        self.assertEqual(
            len(cursor.executed_parameter_sets[0]),
            13,
        )
        self.assertNotIn(
            prepared.content,
            cursor.executed_queries[0],
        )
        self.assertIn(
            "public.rag_api_memory_embedding_succeed",
            cursor.executed_queries[1],
        )

    def test_replay_does_not_depend_on_embedding_service(self) -> None:
        row = self._success_row("create")
        row["replayed"] = True
        cursor = FakeCursor(row)
        repository = MemoryRepository(
            connection_factory=lambda: FakeConnection(cursor),
            embedding_provider=None,
        )
        request = CreateMemoryRequest(
            namespace="project_memory",
            memory_type="fact",
            content="Already committed searchable content.",
            source_type="user_explicit",
            trust_level="high",
            reason="Verify replay remains available.",
            idempotency_key="phase53.repository.replay1",
        )

        result = repository.create(prepare_create(request))

        self.assertTrue(result["replayed"])
        self.assertEqual(len(cursor.executed_queries), 1)

    def test_archive_and_restore_use_fixed_routines(self) -> None:
        archive_cursor = FakeCursor(self._success_row("archive"))
        archive_repository = MemoryRepository(
            connection_factory=lambda: FakeConnection(archive_cursor)
        )

        archive_request = ArchiveMemoryRequest(
            memory_id="11111111-1111-4111-8111-111111111111",
            expected_version=2,
            expected_state_version=3,
            reason="Archive obsolete memory.",
            idempotency_key="phase52.repository.archive1",
        )

        archive_repository.archive(
            prepare_archive(archive_request)
        )

        self.assertIn(
            "public.rag_api_memory_archive",
            archive_cursor.executed_query or "",
        )
        self.assertEqual(
            len(archive_cursor.executed_parameters or ()),
            7,
        )

        restore_cursor = FakeCursor(self._success_row("restore"))
        restore_repository = MemoryRepository(
            connection_factory=lambda: FakeConnection(restore_cursor)
        )

        restore_request = RestoreMemoryRequest(
            memory_id="11111111-1111-4111-8111-111111111111",
            expected_version=2,
            expected_state_version=4,
            reason="Restore archived memory.",
            idempotency_key="phase52.repository.restore1",
        )

        restore_repository.restore(
            prepare_restore(restore_request)
        )

        self.assertIn(
            "public.rag_api_memory_restore_ready",
            restore_cursor.executed_query or "",
        )
        self.assertEqual(
            len(restore_cursor.executed_parameters or ()),
            7,
        )

    def test_database_sqlstate_mapping(self) -> None:
        cases = {
            "P5201": "VALIDATION_ERROR",
            "P5202": "NOT_FOUND",
            "P5203": "CONCURRENCY_CONFLICT",
            "P5204": "INVALID_STATE",
            "P5205": "ALREADY_ARCHIVED",
            "P5206": "NOT_ARCHIVED",
            "P5207": "IDEMPOTENCY_CONFLICT",
            "P5208": "PERMISSION_DENIED",
            "57014": "TIMEOUT",
            "08006": "DATABASE_UNAVAILABLE",
            None: "INTERNAL_ERROR",
        }

        for sqlstate, expected_code in cases.items():
            with self.subTest(sqlstate=sqlstate):
                mapped = _map_database_error(
                    FakeDatabaseError(sqlstate)  # type: ignore[arg-type]
                )
                self.assertEqual(mapped.code, expected_code)
                self.assertNotIn(
                    "database detail",
                    mapped.message,
                )

    def test_runtime_environment_requires_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.env"
            path.write_text(
                "\n".join(
                    [
                        "RAG_MCP_DB_HOST=127.0.0.1",
                        "RAG_MCP_DB_PORT=5432",
                        "RAG_MCP_DB_NAME=ragdb",
                        "RAG_MCP_DB_USER=rag_mcp_runtime",
                        "RAG_MCP_DB_PASSWORD=test-only",
                    ]
                ),
                encoding="utf-8",
            )

            os.chmod(path, 0o644)

            with self.assertRaises(RuntimeError):
                load_runtime_database_settings(path)

            os.chmod(path, 0o600)
            settings = load_runtime_database_settings(path)

            self.assertEqual(settings.host, "127.0.0.1")
            self.assertEqual(settings.port, 5432)
            self.assertEqual(settings.database, "ragdb")
            self.assertEqual(settings.user, "rag_mcp_runtime")

    def test_runtime_environment_rejects_remote_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.env"
            path.write_text(
                "\n".join(
                    [
                        "RAG_MCP_DB_HOST=database.example.com",
                        "RAG_MCP_DB_PORT=5432",
                        "RAG_MCP_DB_NAME=ragdb",
                        "RAG_MCP_DB_USER=rag_mcp_runtime",
                        "RAG_MCP_DB_PASSWORD=test-only",
                    ]
                ),
                encoding="utf-8",
            )

            os.chmod(path, 0o600)

            with self.assertRaises(RuntimeError):
                load_runtime_database_settings(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
