from __future__ import annotations

import asyncio
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


BASE_DIR = Path(__file__).resolve().parent
SERVER_PYTHON = BASE_DIR / ".venv" / "bin" / "python"
SERVER_SCRIPT = BASE_DIR / "rag_mcp_server.py"


async def main() -> None:
    parameters = StdioServerParameters(
        command=str(SERVER_PYTHON),
        args=[str(SERVER_SCRIPT)],
    )

    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            tools_result = await session.list_tools()
            tool_names = [tool.name for tool in tools_result.tools]

            print("===== MCP TOOLS =====")
            print(tool_names)

            expected_tools = {
                "rag_search",
                "rag_status",
                "rag_memory_create",
                "rag_memory_update",
                "rag_memory_archive",
                "rag_memory_restore",
            }

            if set(tool_names) != expected_tools:
                raise RuntimeError(
                    f"Unexpected MCP tools: {tool_names}; "
                    f"expected exactly: {sorted(expected_tools)}"
                )

            print()
            print("===== RAG STATUS CALL =====")

            status_result = await session.call_tool(
                "rag_status",
                {},
            )
            print(
                status_result.model_dump_json(
                    indent=2,
                    exclude_none=True,
                )
            )

            if status_result.isError:
                raise RuntimeError("rag_status returned an MCP tool error.")

            print()
            print("===== RAG SEARCH CALL =====")

            search_result = await session.call_tool(
                "rag_search",
                {
                    "query": "我的 RAG 使用什么 embedding 模型？",
                    "knowledge_base": "tech",
                    "limit": 2,
                },
            )
            print(
                search_result.model_dump_json(
                    indent=2,
                    exclude_none=True,
                )
            )

            if search_result.isError:
                raise RuntimeError("rag_search returned an MCP tool error.")

            print()
            print("MCP smoke test passed.")


if __name__ == "__main__":
    asyncio.run(main())
