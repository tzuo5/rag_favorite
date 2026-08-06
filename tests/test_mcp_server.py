from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from rag_favorite.config import load_config
from rag_favorite.mcp_contracts import MCP_TOOL_NAMES, safe_relative_path
from rag_favorite.mcp_server import create_mcp_server


def _config(tmp_path: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[[collections]]
key = "one"
name = "One"
path = "one"

[[collections]]
key = "two"
name = "Two"
path = "two"
""".strip(),
        encoding="utf-8",
    )
    return load_config(config_file)


def test_packaged_server_has_stable_read_only_tool_set(tmp_path: Path) -> None:
    server = create_mcp_server(_config(tmp_path))

    tools = asyncio.run(server.list_tools())

    assert tuple(sorted(tool.name for tool in tools)) == tuple(sorted(MCP_TOOL_NAMES))
    search = next(tool for tool in tools if tool.name == "rag_search")
    assert set(search.inputSchema["required"]) == {"query", "knowledge_base"}
    assert "include_governed_memory" not in search.inputSchema["properties"]


def test_search_is_direct_bounded_and_redacts_host_paths(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def search_provider(
        query: str,
        limit: int,
        collections: list[str],
        _config: object,
    ) -> list[dict[str, object]]:
        assert query == "deployment"
        assert limit == 3
        calls.append(collections)
        key = collections[0]
        return [
            {
                "result_type": "document",
                "document_id": f"document:{key}",
                "title": f"{key}.md",
                "knowledge_base": key,
                "source_relative_path": f"notes/{key}.md",
                "section": "chunk:0",
                "excerpt": "stored at /home/private/secret/config.toml",
                "semantic_score": 0.8,
                "lexical_score": None,
                "combined_score": 0.8,
                "reliable": True,
                "content_hash": "abc",
                "metadata": {},
            }
        ]

    server = create_mcp_server(_config(tmp_path), search_provider=search_provider)
    tool = server._tool_manager.get_tool("rag_search")

    result = asyncio.run(
        tool.run(
            {
                "query": "deployment",
                "knowledge_base": "one",
                "additional_knowledge_bases": ["two"],
            },
            convert_result=False,
        )
    )

    assert result["ok"] is True
    assert calls == [["one"], ["two"]]
    assert [item["knowledge_base"] for item in result["results"]] == ["one", "two"]
    assert "/home/private" not in str(result)


def test_backend_failure_returns_safe_envelope(tmp_path: Path) -> None:
    def failure(*_args: object) -> list[dict[str, object]]:
        raise RuntimeError("/home/private/database-password")

    server = create_mcp_server(_config(tmp_path), search_provider=failure)
    tool = server._tool_manager.get_tool("rag_search")

    result = asyncio.run(
        tool.run(
            {"query": "deployment", "knowledge_base": "one"},
            convert_result=False,
        )
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "RETRIEVAL_UNAVAILABLE"
    assert "/home/private" not in str(result)


def test_status_exposes_configured_collections_without_host_paths(
    tmp_path: Path,
) -> None:
    def status_provider(_key: str | None, _config: object) -> dict[str, object]:
        return {
            "documents": 1,
            "chunks": 2,
            "embedding_model": "test-model",
            "embedding_dimensions": 3,
            "index_version": 3,
            "last_indexed_at": None,
            "collections": {"one": {"documents": 1, "chunks": 2, "index_version": 3}},
        }

    server = create_mcp_server(_config(tmp_path), status_provider=status_provider)
    tool = server._tool_manager.get_tool("rag_status")

    result = asyncio.run(tool.run({}, convert_result=False))

    assert result["ok"] is True
    assert result["collections"]["one"]["documents"] == 1
    assert result["collections"]["two"]["documents"] == 0
    assert result["available_knowledge_bases"] == {"one": "One", "two": "Two"}


@pytest.mark.parametrize(
    "unsafe",
    ["/home/private/notes.md", "C:\\Users\\private\\notes.md", "../notes.md"],
)
def test_mcp_contract_rejects_host_source_paths(unsafe: str) -> None:
    with pytest.raises(RuntimeError, match="unsafe"):
        safe_relative_path(unsafe)
