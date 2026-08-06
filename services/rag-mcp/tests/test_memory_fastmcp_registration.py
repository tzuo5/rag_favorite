from __future__ import annotations

import asyncio
import unittest
from uuid import UUID

from mcp.server.fastmcp import FastMCP

from memory_api.contracts import MutationResult
from memory_api.fastmcp_registration import (
    MUTATION_TOOL_NAMES,
    register_memory_mutation_tools,
)


MEMORY_ID = UUID("11111111-1111-4111-8111-111111111111")
REVISION_ID = UUID("22222222-2222-4222-8222-222222222222")
REQUEST_ID = UUID("33333333-3333-4333-8333-333333333333")


class FakeService:
    def create(self, request: object) -> MutationResult:
        return self._result("create", "active", revision=True)

    def update(self, request: object) -> MutationResult:
        return self._result("update", "active", revision=True)

    def archive(self, request: object) -> MutationResult:
        return self._result("archive", "archived", revision=False)

    def restore(self, request: object) -> MutationResult:
        return self._result("restore", "active", revision=False)

    def _result(
        self,
        operation: str,
        status: str,
        *,
        revision: bool,
    ) -> MutationResult:
        return MutationResult(
            operation=operation,
            memory_id=MEMORY_ID,
            revision_id=REVISION_ID if revision else None,
            event_id=10,
            current_version=2,
            state_version=4,
            status=status,
            content_hash="a" * 64 if revision else None,
            replayed=False,
            request_id=REQUEST_ID,
        )


class FastMcpRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mcp = FastMCP("Phase 5.2 isolated mutation tools")
        self.service = FakeService()
        self.registered = register_memory_mutation_tools(
            self.mcp,
            self.service,
        )

    def test_exact_tool_names_registered(self) -> None:
        tools = asyncio.run(self.mcp.list_tools())
        names = tuple(sorted(tool.name for tool in tools))

        self.assertEqual(
            names,
            tuple(sorted(MUTATION_TOOL_NAMES)),
        )
        self.assertEqual(self.registered, MUTATION_TOOL_NAMES)

    def test_tool_schema_excludes_governance_fields(self) -> None:
        forbidden = {
            "actor",
            "created_by",
            "updated_by",
            "archived_by",
            "sql",
            "query",
        }

        for name in MUTATION_TOOL_NAMES:
            tool = self.mcp._tool_manager.get_tool(name)
            self.assertIsNotNone(tool)

            properties = set(tool.parameters.get("properties", {}))
            self.assertTrue(forbidden.isdisjoint(properties))

    def test_create_tool_invocation(self) -> None:
        tool = self.mcp._tool_manager.get_tool("rag_memory_create")
        self.assertIsNotNone(tool)

        response = asyncio.run(
            tool.run(
                {
                    "namespace": "project_memory",
                    "memory_type": "decision",
                    "content": "Use governed writes.",
                    "source_type": "user_explicit",
                    "trust_level": "high",
                    "reason": "Explicit project decision.",
                    "idempotency_key": "phase52.fastmcp.create1",
                    "metadata": {
                        "project": "gordon-core-tokyo",
                    },
                },
                convert_result=False,
            )
        )

        self.assertTrue(response["ok"])
        self.assertEqual(response["operation"], "create")
        self.assertEqual(response["memory_id"], str(MEMORY_ID))
        self.assertEqual(response["request_id"], str(REQUEST_ID))

    def test_validation_error_remains_agent_safe(self) -> None:
        tool = self.mcp._tool_manager.get_tool("rag_memory_create")
        self.assertIsNotNone(tool)

        response = asyncio.run(
            tool.run(
                {
                    "namespace": "project_memory",
                    "memory_type": "fact",
                    "content": "SECRET-CONTENT",
                    "source_type": "user_explicit",
                    "trust_level": "high",
                    "reason": "Validation test.",
                    "idempotency_key": "bad key",
                },
                convert_result=False,
            )
        )

        self.assertFalse(response["ok"])
        self.assertEqual(
            response["error"]["code"],
            "VALIDATION_ERROR",
        )
        self.assertNotIn("SECRET-CONTENT", str(response))
        UUID(str(response["request_id"]))

    def test_lifecycle_tool_schemas_require_cas_tokens(self) -> None:
        for name in ("rag_memory_archive", "rag_memory_restore"):
            tool = self.mcp._tool_manager.get_tool(name)
            self.assertIsNotNone(tool)

            required = set(tool.parameters.get("required", []))

            self.assertIn("memory_id", required)
            self.assertIn("expected_version", required)
            self.assertIn("expected_state_version", required)
            self.assertIn("reason", required)
            self.assertIn("idempotency_key", required)


if __name__ == "__main__":
    unittest.main()
