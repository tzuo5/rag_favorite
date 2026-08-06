from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Protocol, TypedDict

from product_config import CONFIG

TOPIC_KNOWLEDGE_BASES = tuple(CONFIG.collections)
ALL_KNOWLEDGE_BASES = TOPIC_KNOWLEDGE_BASES

KNOWLEDGE_BASE_DESCRIPTIONS = {
    key: collection.name for key, collection in CONFIG.collections.items()
}

_HOST_PATH = re.compile(
    r"(?:/home/[^/\s]+|/Users/[^/\s]+)(?:/[^\s)\]}>\"']*)?"
)


class SearchResult(TypedDict):
    result_type: str
    document_id: str
    title: str | None
    knowledge_base: str
    source_relative_path: str
    section: str | None
    excerpt: str | None
    semantic_score: float | None
    lexical_score: float | None
    combined_score: float | None
    reliable: bool
    content_hash: str | None
    metadata: dict[str, Any]


class StatusResult(TypedDict):
    healthy: bool
    knowledge_base: str
    documents: int
    chunks: int
    embedding_model: str | None
    embedding_dimensions: int | None
    index_version: int | None
    last_indexed_at: str | None


class RetrievalAdapter(Protocol):
    def search(
        self,
        query: str,
        knowledge_bases: list[str],
        limit: int,
    ) -> list[SearchResult]: ...

    def status(self, knowledge_base: str) -> StatusResult: ...


def safe_relative_path(value: object) -> str:
    """Validate a backend source label before exposing it to an agent."""

    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("Retrieval backend returned an invalid source label.")
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError("Retrieval backend returned an unsafe source label.")
    return path.as_posix()


def redact_host_paths(value: object) -> str | None:
    if value is None:
        return None
    return _HOST_PATH.sub("[host-path-redacted]", str(value))


def validate_search_result(result: SearchResult) -> SearchResult:
    if result["knowledge_base"] not in ALL_KNOWLEDGE_BASES:
        raise RuntimeError("Retrieval backend returned an unknown knowledge base.")
    if not result["document_id"]:
        raise RuntimeError("Retrieval backend returned an invalid document id.")
    result["source_relative_path"] = safe_relative_path(result["source_relative_path"])
    result["title"] = redact_host_paths(result["title"])
    result["section"] = redact_host_paths(result["section"])
    result["excerpt"] = redact_host_paths(result["excerpt"])
    return result
