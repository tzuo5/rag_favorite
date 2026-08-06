from __future__ import annotations

import json
import unittest
from uuid import UUID

from memory_api.contracts import (
    ArchiveMemoryRequest,
    CreateMemoryRequest,
    MutationResult,
    RestoreMemoryRequest,
    UpdateMemoryRequest,
)
from memory_api.errors import DomainError
from memory_api.mcp_adapter import (
    handle_memory_archive,
    handle_memory_create,
    handle_memory_restore,
    handle_memory_update,
)


MEMORY_ID = UUID("11111111-1111-4111-8111-111111111111")
REVISION_ID = UUID("22222222-2222-4222-8222-222222222222")
RESULT_REQUEST_ID = UUID("33333333-3333-4333-8333-333333333333")
CONTENT_HASH = "a" * 64


def build_result(operation: str) -> MutationResult:
    lifecycle = operation in {"archive", "restore"}

    return MutationResult(
        operation=operation,
        memory_id=MEMORY_ID,
        revision_id=None if lifecycle else REVISION_ID,
        event_id=10,
        current_version=2,
        state_version=4,
        status="archived" if operation == "archive" else "active",
        content_hash=None if lifecycle else CONTENT_HASH,
        replayed=False,
        request_id=RESULT_REQUEST_ID,
    )


class FakeService:
    def __init__(
        self,
        *,
        domain_error: DomainError | None = None,
        unexpected_error: Exception | None = None,
    ) -> None:
        self.domain_error = domain_error
        self.unexpected_error = unexpected_error
        self.calls: list[tuple[str, object]] = []

    def _execute(self, operation: str, request: object) -> MutationResult:
        self.calls.append((operation, request))

        if self.domain_error is not None:
            raise self.domain_error

        if self.unexpected_error is not None:
            raise self.unexpected_error

        return build_result(operation)

    def create(self, request: CreateMemoryRequest) -> MutationResult:
        return self._execute("create", request)

    def update(self, request: UpdateMemoryRequest) -> MutationResult:
        return self._execute("update", request)

    def archive(self, request: ArchiveMemoryRequest) -> MutationResult:
        return self._execute("archive", request)

    def restore(self, request: RestoreMemoryRequest) -> MutationResult:
        return self._execute("restore", request)


class MemoryMcpAdapterTests(unittest.TestCase):
    def test_valid_create(self) -> None:
        service = FakeService()
        response = handle_memory_create(
            {
                "namespace": "project_memory",
                "memory_type": "decision",
                "content": "Use governed writes.",
                "source_type": "user_explicit",
                "trust_level": "high",
                "reason": "User explicitly approved the design.",
                "idempotency_key": "phase52.adapter.create1",
                "metadata": {"project": "gordon-core-tokyo"},
            },
            service,
        )

        self.assertTrue(response["ok"])
        self.assertEqual(response["operation"], "create")
        self.assertEqual(response["request_id"], str(RESULT_REQUEST_ID))
        self.assertIsInstance(service.calls[0][1], CreateMemoryRequest)

    def test_valid_update(self) -> None:
        service = FakeService()
        response = handle_memory_update(
            {
                "memory_id": str(MEMORY_ID),
                "expected_version": 1,
                "expected_state_version": 1,
                "content": "Updated governed content.",
                "source_type": "user_explicit",
                "trust_level": "high",
                "reason": "Correct prior content.",
                "idempotency_key": "phase52.adapter.update1",
                "metadata": {"language": "en"},
            },
            service,
        )

        self.assertTrue(response["ok"])
        self.assertEqual(response["operation"], "update")
        self.assertIsInstance(service.calls[0][1], UpdateMemoryRequest)

    def test_valid_archive(self) -> None:
        service = FakeService()
        response = handle_memory_archive(
            {
                "memory_id": str(MEMORY_ID),
                "expected_version": 2,
                "expected_state_version": 3,
                "reason": "No longer current.",
                "idempotency_key": "phase52.adapter.archive1",
            },
            service,
        )

        self.assertTrue(response["ok"])
        self.assertEqual(response["operation"], "archive")
        self.assertEqual(response["status"], "archived")
        self.assertIsInstance(service.calls[0][1], ArchiveMemoryRequest)

    def test_valid_restore(self) -> None:
        service = FakeService()
        response = handle_memory_restore(
            {
                "memory_id": str(MEMORY_ID),
                "expected_version": 2,
                "expected_state_version": 4,
                "reason": "Memory is current again.",
                "idempotency_key": "phase52.adapter.restore1",
            },
            service,
        )

        self.assertTrue(response["ok"])
        self.assertEqual(response["operation"], "restore")
        self.assertEqual(response["status"], "active")
        self.assertIsInstance(service.calls[0][1], RestoreMemoryRequest)

    def test_validation_error_is_redacted(self) -> None:
        secret = "TOP-SECRET-PLAINTEXT-CONTENT"
        service = FakeService()

        response = handle_memory_create(
            {
                "namespace": "project_memory",
                "memory_type": "fact",
                "content": secret,
                "source_type": "user_explicit",
                "trust_level": "high",
                "reason": "Invalid request test.",
                "idempotency_key": "bad key",
            },
            service,
        )

        serialized = json.dumps(response, sort_keys=True)

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "VALIDATION_ERROR")
        self.assertNotIn(secret, serialized)
        self.assertEqual(service.calls, [])
        UUID(str(response["request_id"]))

    def test_domain_error_is_preserved(self) -> None:
        service = FakeService(
            domain_error=DomainError(
                "CONCURRENCY_CONFLICT",
                field="expected_state_version",
                retryable=True,
            )
        )

        response = handle_memory_archive(
            {
                "memory_id": str(MEMORY_ID),
                "expected_version": 2,
                "expected_state_version": 3,
                "reason": "Concurrent update test.",
                "idempotency_key": "phase52.adapter.domain1",
            },
            service,
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "CONCURRENCY_CONFLICT")
        self.assertEqual(response["error"]["field"], "expected_state_version")
        self.assertTrue(response["error"]["retryable"])
        UUID(str(response["request_id"]))

    def test_unexpected_error_is_redacted(self) -> None:
        leaked = "SELECT password FROM secrets /home/ubuntu/private.py"
        service = FakeService(unexpected_error=RuntimeError(leaked))

        response = handle_memory_restore(
            {
                "memory_id": str(MEMORY_ID),
                "expected_version": 2,
                "expected_state_version": 4,
                "reason": "Unexpected failure test.",
                "idempotency_key": "phase52.adapter.internal1",
            },
            service,
        )

        serialized = json.dumps(response, sort_keys=True)

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "INTERNAL_ERROR")
        self.assertNotIn(leaked, serialized)
        self.assertNotIn("SELECT", serialized)
        self.assertNotIn("/home/", serialized)
        UUID(str(response["request_id"]))

    def test_extra_actor_field_is_rejected(self) -> None:
        service = FakeService()

        response = handle_memory_archive(
            {
                "memory_id": str(MEMORY_ID),
                "expected_version": 2,
                "expected_state_version": 3,
                "reason": "Extra field test.",
                "idempotency_key": "phase52.adapter.extra1",
                "actor": "attacker-controlled",
            },
            service,
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "VALIDATION_ERROR")
        self.assertEqual(service.calls, [])

    def test_all_paths_include_request_id(self) -> None:
        valid_payload = {
            "namespace": "project_memory",
            "memory_type": "fact",
            "content": "Request ID test.",
            "source_type": "user_explicit",
            "trust_level": "high",
            "reason": "Verify request ID.",
            "idempotency_key": "phase52.adapter.request1",
        }

        responses = [
            handle_memory_create(valid_payload, FakeService()),
            handle_memory_create({}, FakeService()),
            handle_memory_create(
                valid_payload,
                FakeService(domain_error=DomainError("PERMISSION_DENIED")),
            ),
            handle_memory_create(
                valid_payload,
                FakeService(unexpected_error=RuntimeError("failure")),
            ),
        ]

        for response in responses:
            UUID(str(response["request_id"]))


if __name__ == "__main__":
    unittest.main()
