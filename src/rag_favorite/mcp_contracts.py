"""Stable, agent-safe contracts for the packaged MCP server."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

MAX_QUERY_CHARACTERS = 2_000
MIN_SEARCH_LIMIT = 1
MAX_SEARCH_LIMIT = 10
MCP_TOOL_NAMES = ("rag_search", "rag_status")

_HOST_PATH = re.compile(
    r"(?:/home/[^/\s]+|/Users/[^/\s]+)(?:/[^\s)\]}>\"']*)?"
)
_WINDOWS_HOST_PATH = re.compile(
    r"(?i)\b[A-Z]:\\Users\\[^\\\s]+(?:\\[^\s)\]}>\"']*)?"
)


def safe_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("Retrieval backend returned an invalid source label.")
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or normalized.startswith("//")
        or re.match(r"^[A-Za-z]:/", normalized)
        or ".." in path.parts
    ):
        raise RuntimeError("Retrieval backend returned an unsafe source label.")
    return path.as_posix()


def redact_host_paths(value: object) -> str | None:
    if value is None:
        return None
    redacted = _HOST_PATH.sub("[host-path-redacted]", str(value))
    return _WINDOWS_HOST_PATH.sub("[host-path-redacted]", redacted)


def validate_search_result(
    raw: dict[str, object], configured_collections: set[str]
) -> dict[str, Any]:
    knowledge_base = str(raw.get("knowledge_base") or "")
    if knowledge_base not in configured_collections:
        raise RuntimeError("Retrieval backend returned an unknown knowledge base.")
    document_id = str(raw.get("document_id") or "")
    if not document_id:
        raise RuntimeError("Retrieval backend returned an invalid document id.")
    metadata = raw.get("metadata")
    return {
        "result_type": str(raw.get("result_type") or "document"),
        "document_id": document_id,
        "title": redact_host_paths(raw.get("title")),
        "knowledge_base": knowledge_base,
        "source_relative_path": safe_relative_path(raw.get("source_relative_path")),
        "section": redact_host_paths(raw.get("section")),
        "excerpt": redact_host_paths(raw.get("excerpt")),
        "semantic_score": raw.get("semantic_score"),
        "lexical_score": raw.get("lexical_score"),
        "combined_score": raw.get("combined_score"),
        "reliable": bool(raw.get("reliable")),
        "content_hash": raw.get("content_hash"),
        "metadata": dict(metadata) if isinstance(metadata, dict) else {},
    }


def merge_ranked_results(
    groups: list[list[dict[str, Any]]], limit: int
) -> list[dict[str, Any]]:
    """Round-robin rankings whose score scales may not be comparable."""

    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    rank = 0
    while len(merged) < limit:
        added = False
        for group in groups:
            if rank >= len(group):
                continue
            added = True
            item = group[rank]
            identity = (item["document_id"], item["section"])
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(item)
            if len(merged) >= limit:
                break
        if not added:
            break
        rank += 1
    return merged
