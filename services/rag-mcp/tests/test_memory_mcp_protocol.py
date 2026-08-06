from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import (
    StdioServerParameters,
    stdio_client,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_FILE = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "phase52_protocol_server.py"
)

EXPECTED_TOOLS = {
    "rag_memory_create",
    "rag_memory_update",
    "rag_memory_archive",
    "rag_memory_restore",
}


class MemoryMcpProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_initialize_list_and_call(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT_ROOT)

        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER_FILE)],
            env=environment,
            cwd=str(PROJECT_ROOT),
        )

        async with stdio_client(parameters) as streams:
            read_stream, write_stream = streams

            async with ClientSession(
                read_stream,
                write_stream,
            ) as session:
                initialization = await session.initialize()

                self.assertIsNotNone(initialization.serverInfo)

                tool_result = await session.list_tools()
                names = {tool.name for tool in tool_result.tools}

                self.assertEqual(names, EXPECTED_TOOLS)

                create_result = await session.call_tool(
                    "rag_memory_create",
                    arguments={
                        "namespace": "project_memory",
                        "memory_type": "decision",
                        "content": "Protocol fixture content.",
                        "source_type": "user_explicit",
                        "trust_level": "high",
                        "reason": "Validate real MCP stdio transport.",
                        "idempotency_key": "phase52.protocol.create1",
                        "metadata": {
                            "project": "gordon-core-tokyo",
                        },
                    },
                )

                self.assertFalse(create_result.isError)
                self.assertIsInstance(
                    create_result.structuredContent,
                    dict,
                )

                payload = create_result.structuredContent

                self.assertTrue(payload["ok"])
                self.assertEqual(payload["operation"], "create")
                self.assertEqual(payload["current_version"], 1)
                self.assertEqual(payload["state_version"], 1)
                self.assertEqual(payload["status"], "active")
                self.assertFalse(payload["replayed"])
                self.assertEqual(
                    payload["memory_id"],
                    "11111111-1111-4111-8111-111111111111",
                )


if __name__ == "__main__":
    unittest.main()
