from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    "cooking_recipe_create",
    "cooking_recipe_search",
    "cooking_recipe_get",
    "cooking_rag_status",
}


@unittest.skipUnless(
    os.environ.get("COOKING_RAG_PROTOCOL_TEST") == "1",
    "requires an isolated cooking RAG test database",
)
class CookingMcpProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_initialize_list_and_status(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT_ROOT)
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(PROJECT_ROOT / "mcp_server.py")],
            env=environment,
            cwd=str(PROJECT_ROOT),
        )

        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = await session.list_tools()
                self.assertEqual({tool.name for tool in tools.tools}, EXPECTED_TOOLS)
                result = await session.call_tool("cooking_rag_status", arguments={})
                self.assertFalse(result.isError)
                self.assertGreaterEqual(result.structuredContent["recipes"], 1)
                self.assertGreaterEqual(result.structuredContent["sections"], 1)


if __name__ == "__main__":
    unittest.main()
