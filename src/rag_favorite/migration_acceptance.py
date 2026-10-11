"""Real stdio MCP acceptance before the migration opens the ingestion queue."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import timedelta

from .config import ConfigError
from .knowledge_summaries import text_hash


def payload(result):
    if result.isError:
        raise ConfigError("MCP tool returned an error.")
    if isinstance(result.structuredContent, dict):
        return result.structuredContent
    for block in result.content:
        if block.type == "text":
            return json.loads(block.text)
    raise ConfigError("MCP returned no structured result.")


async def _verify(config, video):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)
    env["RAG_FAVORITE_CONFIG"] = str(config.source)
    env["RAG_VIDEO_CONFIG"] = str(video.source)
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "rag_favorite.video_mcp"], env=env
    )
    report = {"transport": "real_stdio", "queries": []}
    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=180)
        ) as session,
    ):
        await session.initialize()
        definitions = await session.list_tools()
        names = {tool.name for tool in definitions.tools}
        if not {"rag_search", "document_read", "rag_status"}.issubset(names):
            raise ConfigError("MCP knowledge tools are missing.")
        status = payload(await session.call_tool("rag_status", {}))
        if not status.get("ok") or not status.get("documents"):
            raise ConfigError("MCP reports an empty or unavailable library.")
        report["published_documents"] = status["documents"]
        for query in ["长岛冰茶", "什么是API", "如何通过领英获得内推"]:
            result = payload(await session.call_tool("rag_search", {"query": query}))
            rows = result.get("results", [])
            if (
                not result.get("ok")
                or not rows
                or len(rows) > 5
                or len({r["document_id"] for r in rows}) != len(rows)
            ):
                raise ConfigError("MCP summary retrieval acceptance failed.")
            if not all(row["knowledge_base"] == "general" for row in rows):
                raise ConfigError("MCP retained a topic retrieval partition.")
            alias = payload(
                await session.call_tool(
                    "rag_search", {"query": query, "knowledge_base": "cooking"}
                )
            )
            if [r["document_id"] for r in alias["results"]] != [
                r["document_id"] for r in rows
            ]:
                raise ConfigError("Legacy topic alias changed the search scope.")
            selected = rows[0]
            offset = 0
            original = []
            pages = 0
            while True:
                page = payload(
                    await session.call_tool(
                        "document_read",
                        {
                            "document_id": selected["document_id"],
                            "version": selected["metadata"]["version"],
                            "offset": offset,
                            "max_characters": 16000,
                        },
                    )
                )
                if not page.get("ok"):
                    raise ConfigError("MCP original reading failed.")
                original.append(page["original_document"])
                pages += 1
                if page["complete"]:
                    break
                if page["next_offset"] <= offset:
                    raise ConfigError("MCP pagination did not advance.")
                offset = page["next_offset"]
            if text_hash("".join(original)) != page["draft_sha256"]:
                raise ConfigError("MCP original hash check failed.")
            report["queries"].append(
                {
                    "query": query,
                    "results": len(rows),
                    "document_id": selected["document_id"],
                    "original_pages": pages,
                    "original_hash_verified": True,
                }
            )
    return report


def verify(config, video):
    return asyncio.run(_verify(config, video))
