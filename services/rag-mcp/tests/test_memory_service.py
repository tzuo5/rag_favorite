from __future__ import annotations

import unittest
from uuid import UUID

from memory_api.contracts import (
    ArchiveMemoryRequest,
    CreateMemoryRequest,
    RestoreMemoryRequest,
    UpdateMemoryRequest,
)
from memory_api.errors import DomainError
from memory_api.service import (
    MemoryService,
    PreparedCreate,
    PreparedLifecycle,
    PreparedUpdate,
)


FIXED_REQUEST_ID = UUID(
    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
)

MEMORY_ID = UUID(
    "11111111-1111-4111-8111-111111111111"
)

REVISION_ID = UUID(
    "22222222-2222-4222-8222-222222222222"
)


class FakeRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.rows: dict[str, dict[str, object]] = {
            operation: {
                "operation": operation,
                "memory_id": MEMORY_ID,
                "revision_id": REVISION_ID,
                "event_id": 1,
                "current_version": 1,
                "state_version": 1,
                "status": "active",
                "content_hash": "a" * 64,
                "replayed": False,
            }
            for operation in (
                "create",
                "update",
                "archive",
                "restore",
            )
        }

    def create(
        self,
        prepared: PreparedCreate,
    ) -> dict[str, object]:
        self.calls.append(("create", prepared))
        return self.rows["create"]

    def update(
        self,
        prepared: PreparedUpdate,
    ) -> dict[str, object]:
        self.calls.append(("update", prepared))
        return self.rows["update"]

    def archive(
        self,
        prepared: PreparedLifecycle,
    ) -> dict[str, object]:
        self.calls.append(("archive", prepared))
        return self.rows["archive"]

    def restore(
        self,
        prepared: PreparedLifecycle,
    ) -> dict[str, object]:
        self.calls.append(("restore", prepared))
        return self.rows["restore"]


class MemoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = FakeRepository()
        self.service = MemoryService(
            self.repository,
            request_id_factory=lambda: FIXED_REQUEST_ID,
        )

    def test_create_orchestration(self) -> None:
        request = CreateMemoryRequest(
            namespace="project_memory",
            memory_type="decision",
            content="  Use governed writes.\r\n",
            source_type="user_explicit",
            trust_level="high",
            reason="User explicitly approved this decision.",
            idempotency_key="phase52.service.create1",
            metadata={
                "project": "gordon-core-tokyo",
            },
        )

        result = self.service.create(request)

        self.assertEqual(result.operation, "create")
        self.assertEqual(result.request_id, FIXED_REQUEST_ID)
        self.assertEqual(result.memory_id, MEMORY_ID)
        self.assertEqual(len(self.repository.calls), 1)

        operation, prepared = self.repository.calls[0]

        self.assertEqual(operation, "create")
        self.assertIsInstance(prepared, PreparedCreate)
        self.assertEqual(
            prepared.content,  # type: ignore[union-attr]
            "Use governed writes.",
        )

    def test_update_orchestration(self) -> None:
        request = UpdateMemoryRequest(
            memory_id=MEMORY_ID,
            expected_version=1,
            expected_state_version=1,
            content="Updated governed content.",
            source_type="user_explicit",
            trust_level="high",
            reason="Correct prior content.",
            idempotency_key="phase52.service.update1",
        )

        result = self.service.update(request)

        self.assertEqual(result.operation, "update")
        self.assertEqual(self.repository.calls[0][0], "update")
        self.assertIsInstance(
            self.repository.calls[0][1],
            PreparedUpdate,
        )

    def test_archive_orchestration(self) -> None:
        request = ArchiveMemoryRequest(
            memory_id=MEMORY_ID,
            expected_version=2,
            expected_state_version=3,
            reason="No longer current.",
            idempotency_key="phase52.service.archive1",
        )

        result = self.service.archive(request)

        self.assertEqual(result.operation, "archive")
        self.assertEqual(self.repository.calls[0][0], "archive")
        self.assertIsInstance(
            self.repository.calls[0][1],
            PreparedLifecycle,
        )

    def test_restore_orchestration(self) -> None:
        request = RestoreMemoryRequest(
            memory_id=MEMORY_ID,
            expected_version=2,
            expected_state_version=4,
            reason="Memory is current again.",
            idempotency_key="phase52.service.restore1",
        )

        result = self.service.restore(request)

        self.assertEqual(result.operation, "restore")
        self.assertEqual(self.repository.calls[0][0], "restore")

    def test_replayed_result_is_preserved(self) -> None:
        self.repository.rows["create"]["replayed"] = True

        request = CreateMemoryRequest(
            namespace="project_memory",
            memory_type="fact",
            content="Replay-safe content.",
            source_type="user_explicit",
            trust_level="high",
            reason="Test replay behavior.",
            idempotency_key="phase52.service.replay1",
        )

        result = self.service.create(request)

        self.assertTrue(result.replayed)

    def test_wrong_repository_operation_is_internal_error(self) -> None:
        self.repository.rows["create"]["operation"] = "update"

        request = CreateMemoryRequest(
            namespace="project_memory",
            memory_type="fact",
            content="Invalid repository response.",
            source_type="user_explicit",
            trust_level="high",
            reason="Test repository result validation.",
            idempotency_key="phase52.service.invalid1",
        )

        with self.assertRaises(DomainError) as context:
            self.service.create(request)

        self.assertEqual(
            context.exception.code,
            "INTERNAL_ERROR",
        )

    def test_invalid_repository_payload_is_internal_error(self) -> None:
        del self.repository.rows["create"]["event_id"]

        request = CreateMemoryRequest(
            namespace="project_memory",
            memory_type="fact",
            content="Incomplete repository response.",
            source_type="user_explicit",
            trust_level="high",
            reason="Test repository payload validation.",
            idempotency_key="phase52.service.invalid2",
        )

        with self.assertRaises(DomainError) as context:
            self.service.create(request)

        self.assertEqual(
            context.exception.code,
            "INTERNAL_ERROR",
        )

    def test_domain_error_envelope_is_redacted(self) -> None:
        error = DomainError(
            "DATABASE_UNAVAILABLE",
            retryable=True,
        )

        envelope = error.to_dict(
            str(FIXED_REQUEST_ID)
        )

        self.assertEqual(
            envelope["error"]["code"],  # type: ignore[index]
            "DATABASE_UNAVAILABLE",
        )

        serialized = repr(envelope)

        self.assertNotIn("password", serialized.lower())
        self.assertNotIn("sql", serialized.lower())
        self.assertNotIn("/home/", serialized)


if __name__ == "__main__":
    unittest.main(verbosity=2)
