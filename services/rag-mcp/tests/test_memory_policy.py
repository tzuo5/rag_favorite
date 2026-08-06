from __future__ import annotations

import unittest
from uuid import UUID

from pydantic import ValidationError

from memory_api.contracts import (
    ArchiveMemoryRequest,
    CreateMemoryRequest,
    UpdateMemoryRequest,
)
from memory_api.policy import (
    build_request_hash,
    content_sha256,
    derive_initial_status,
    normalize_content,
)
from memory_api.service import (
    prepare_archive,
    prepare_create,
    prepare_update,
)


class ContractPolicyTests(unittest.TestCase):
    def test_content_normalization(self) -> None:
        value = "\ufeff  line one\r\nline two\r  "

        self.assertEqual(
            normalize_content(value),
            "line one\nline two",
        )

    def test_content_hash_is_deterministic(self) -> None:
        first = content_sha256("  alpha\r\nbeta  ")
        second = content_sha256("alpha\nbeta")

        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")

    def test_high_trust_user_explicit_is_active(self) -> None:
        request = CreateMemoryRequest(
            namespace="project_memory",
            memory_type="decision",
            content="Use governed writes.",
            source_type="user_explicit",
            trust_level="high",
            reason="User explicitly approved the design.",
            idempotency_key="phase52.create.test1",
            metadata={"project": "gordon-core-tokyo"},
        )

        self.assertEqual(derive_initial_status(request), "active")

    def test_agent_inference_is_candidate(self) -> None:
        request = CreateMemoryRequest(
            namespace="agent_observations",
            memory_type="observation",
            content="Possible project preference.",
            source_type="agent_inference",
            trust_level="medium",
            reason="Derived from repeated interactions.",
            idempotency_key="phase52.create.test2",
        )

        self.assertEqual(derive_initial_status(request), "candidate")

    def test_quarantine_requires_untrusted(self) -> None:
        with self.assertRaises(ValidationError):
            CreateMemoryRequest(
                namespace="quarantine",
                memory_type="reference",
                content="Unverified statement.",
                source_type="external_document",
                trust_level="medium",
                reason="Pending verification.",
                idempotency_key="phase52.create.test3",
            )

    def test_high_trust_requires_user_explicit(self) -> None:
        with self.assertRaises(ValidationError):
            CreateMemoryRequest(
                namespace="project_memory",
                memory_type="decision",
                content="Inferred decision.",
                source_type="agent_inference",
                trust_level="high",
                reason="Agent inference.",
                idempotency_key="phase52.create.test4",
            )

    def test_unknown_metadata_key_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            CreateMemoryRequest(
                namespace="project_memory",
                memory_type="fact",
                content="Test memory.",
                source_type="user_explicit",
                trust_level="high",
                reason="Testing metadata validation.",
                idempotency_key="phase52.create.test5",
                metadata={"forbidden": "value"},
            )

    def test_duplicate_tags_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            CreateMemoryRequest(
                namespace="project_memory",
                memory_type="fact",
                content="Test memory.",
                source_type="user_explicit",
                trust_level="high",
                reason="Testing duplicate tags.",
                idempotency_key="phase52.create.test6",
                metadata={"tags": ["one", "one"]},
            )

    def test_invalid_idempotency_key_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            CreateMemoryRequest(
                namespace="project_memory",
                memory_type="fact",
                content="Test memory.",
                source_type="user_explicit",
                trust_level="high",
                reason="Testing idempotency validation.",
                idempotency_key="bad key",
            )

    def test_prepare_create_does_not_hash_raw_content(self) -> None:
        request = CreateMemoryRequest(
            namespace="project_memory",
            memory_type="decision",
            content="  alpha\r\nbeta  ",
            source_type="user_explicit",
            trust_level="high",
            reason="Canonicalization test.",
            idempotency_key="phase52.create.test7",
        )

        prepared = prepare_create(request)

        self.assertEqual(prepared.content, "alpha\nbeta")
        self.assertEqual(
            prepared.content_hash,
            content_sha256("alpha\nbeta"),
        )
        self.assertEqual(prepared.initial_status, "active")
        self.assertIsInstance(prepared.memory_id, UUID)
        self.assertIsInstance(prepared.revision_id, UUID)

    def test_request_hash_is_order_independent_for_metadata(self) -> None:
        common = {
            "operation": "create",
            "memory_id": None,
            "expected_version": None,
            "expected_state_version": None,
            "namespace": "project_memory",
            "memory_type": "fact",
            "normalized_content_hash": "a" * 64,
            "source_type": "user_explicit",
            "source_ref": None,
            "trust_level": "high",
            "reason": "Test hash.",
        }

        first = build_request_hash(
            **common,
            metadata={
                "project": "gordon",
                "tags": ["one", "two"],
            },
        )

        second = build_request_hash(
            **common,
            metadata={
                "tags": ["one", "two"],
                "project": "gordon",
            },
        )

        self.assertEqual(first, second)

    def test_request_hash_excludes_plaintext_content(self) -> None:
        secret_text = "plaintext-memory-content"

        request_hash = build_request_hash(
            operation="create",
            memory_id=None,
            expected_version=None,
            expected_state_version=None,
            namespace="project_memory",
            memory_type="fact",
            normalized_content_hash=content_sha256(secret_text),
            source_type="user_explicit",
            source_ref=None,
            trust_level="high",
            reason="Fingerprint test.",
            metadata={},
        )

        self.assertNotIn(secret_text, request_hash)
        self.assertRegex(request_hash, r"^[0-9a-f]{64}$")

    def test_prepare_update(self) -> None:
        request = UpdateMemoryRequest(
            memory_id="11111111-1111-4111-8111-111111111111",
            expected_version=2,
            expected_state_version=4,
            content="Updated content",
            source_type="user_explicit",
            trust_level="high",
            reason="Correct prior content.",
            idempotency_key="phase52.update.test1",
            metadata={"language": "en"},
        )

        prepared = prepare_update(request)

        self.assertEqual(prepared.expected_version, 2)
        self.assertEqual(prepared.expected_state_version, 4)
        self.assertEqual(prepared.content, "Updated content")
        self.assertRegex(prepared.request_hash, r"^[0-9a-f]{64}$")

    def test_prepare_archive(self) -> None:
        request = ArchiveMemoryRequest(
            memory_id="11111111-1111-4111-8111-111111111111",
            expected_version=2,
            expected_state_version=4,
            reason="No longer current.",
            idempotency_key="phase52.archive.test1",
        )

        prepared = prepare_archive(request)

        self.assertEqual(prepared.expected_version, 2)
        self.assertEqual(prepared.expected_state_version, 4)
        self.assertRegex(prepared.request_hash, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
