from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Protocol

from retrieval_contracts import (
    TOPIC_KNOWLEDGE_BASES,
    SearchResult,
    StatusResult,
    validate_search_result,
)

COOKING_INDEX_VERSION = 3


class CommandRunner(Protocol):
    def __call__(
        self,
        arguments: list[str],
        *,
        timeout_seconds: int,
    ) -> str: ...


def _json_object(output: str) -> dict[str, Any]:
    try:
        value = json.loads(output)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Retrieval backend returned an invalid structured response."
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeError("Retrieval backend returned an invalid structured response.")
    return value


class TopicRetrievalAdapter:
    def __init__(
        self,
        runner: CommandRunner,
        *,
        search_timeout_seconds: int,
        status_timeout_seconds: int,
    ) -> None:
        self.runner = runner
        self.search_timeout_seconds = search_timeout_seconds
        self.status_timeout_seconds = status_timeout_seconds

    def search(
        self,
        query: str,
        knowledge_bases: list[str],
        limit: int,
    ) -> list[SearchResult]:
        invalid = [
            value for value in knowledge_bases if value not in TOPIC_KNOWLEDGE_BASES
        ]
        if invalid or not knowledge_bases:
            raise RuntimeError("Topic adapter received an invalid knowledge base.")

        arguments = ["search", query, "--limit", str(limit)]
        for knowledge_base in knowledge_bases:
            arguments.extend(["--knowledge-base", knowledge_base])
        arguments.append("--json")
        payload = _json_object(
            self.runner(
                arguments,
                timeout_seconds=self.search_timeout_seconds,
            )
        )
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise RuntimeError("Topic retrieval returned an invalid result collection.")

        results: list[SearchResult] = []
        for raw in raw_results:
            if not isinstance(raw, dict):
                raise RuntimeError("Topic retrieval returned an invalid result.")
            result = SearchResult(
                result_type=str(raw.get("result_type") or "document"),
                document_id=str(raw.get("document_id") or ""),
                title=raw.get("title"),
                knowledge_base=str(raw.get("knowledge_base") or ""),
                source_relative_path=str(raw.get("source_relative_path") or ""),
                section=raw.get("section"),
                excerpt=raw.get("excerpt"),
                semantic_score=_optional_float(raw.get("semantic_score")),
                lexical_score=_optional_float(raw.get("lexical_score")),
                combined_score=_optional_float(raw.get("combined_score")),
                reliable=bool(raw.get("reliable")),
                content_hash=(
                    str(raw["content_hash"])
                    if raw.get("content_hash") is not None
                    else None
                ),
                metadata=(
                    dict(raw["metadata"])
                    if isinstance(raw.get("metadata"), dict)
                    else {}
                ),
            )
            results.append(validate_search_result(result))
        return results

    def status(self, knowledge_base: str) -> StatusResult:
        if knowledge_base not in TOPIC_KNOWLEDGE_BASES:
            raise RuntimeError("Topic adapter received an invalid knowledge base.")
        payload = _json_object(
            self.runner(
                ["status", "--knowledge-base", knowledge_base, "--json"],
                timeout_seconds=self.status_timeout_seconds,
            )
        )
        return _status_result(payload, knowledge_base)

    def all_statuses(self) -> dict[str, StatusResult]:
        payload = _json_object(
            self.runner(
                ["status", "--json"],
                timeout_seconds=self.status_timeout_seconds,
            )
        )
        collections = payload.get("collections")
        if not isinstance(collections, dict):
            raise RuntimeError("Topic status returned an invalid collection.")
        results: dict[str, StatusResult] = {}
        for knowledge_base in TOPIC_KNOWLEDGE_BASES:
            raw = collections.get(knowledge_base)
            if raw is None:
                raw = {
                    "documents": 0,
                    "chunks": 0,
                    "last_indexed_at": None,
                    "index_version": payload.get("index_version"),
                }
            if not isinstance(raw, dict):
                raise RuntimeError("Topic status returned an invalid collection.")
            results[knowledge_base] = _status_result(
                {
                    **raw,
                    "embedding_model": payload.get("embedding_model"),
                    "embedding_dimensions": payload.get("embedding_dimensions"),
                },
                knowledge_base,
            )
        return results


class CookingRetrievalAdapter:
    def __init__(
        self,
        runner: CommandRunner,
        *,
        search_timeout_seconds: int,
        status_timeout_seconds: int,
    ) -> None:
        self.runner = runner
        self.search_timeout_seconds = search_timeout_seconds
        self.status_timeout_seconds = status_timeout_seconds

    def search(
        self,
        query: str,
        knowledge_bases: list[str],
        limit: int,
    ) -> list[SearchResult]:
        if knowledge_bases != ["cooking"]:
            raise RuntimeError("Cooking adapter received an invalid knowledge base.")
        output = self.runner(
            ["search", query, "--limit", str(limit)],
            timeout_seconds=self.search_timeout_seconds,
        )
        try:
            payload = json.loads(output)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "Cooking retrieval returned an invalid structured response."
            ) from exc
        if not isinstance(payload, list):
            raise RuntimeError(
                "Cooking retrieval returned an invalid result collection."
            )

        results: list[SearchResult] = []
        for raw in payload:
            if not isinstance(raw, dict):
                raise RuntimeError("Cooking retrieval returned an invalid result.")
            if not bool(raw.get("reliable")):
                continue
            recipe_id = int(raw["recipe_id"])
            result = SearchResult(
                result_type="recipe",
                document_id=f"cooking:{recipe_id}",
                title=raw.get("title"),
                knowledge_base="cooking",
                source_relative_path=str(raw.get("source_path") or ""),
                section=raw.get("heading_path"),
                excerpt=raw.get("excerpt"),
                semantic_score=_optional_float(raw.get("cosine_similarity")),
                lexical_score=_optional_float(raw.get("lexical_similarity")),
                combined_score=_optional_float(raw.get("hybrid_score")),
                reliable=True,
                content_hash=None,
                metadata={
                    "recipe_id": recipe_id,
                    "cuisine": raw.get("cuisine"),
                    "category": raw.get("category"),
                    "document_type": raw.get("document_type"),
                },
            )
            results.append(validate_search_result(result))
        return results

    def status(self, knowledge_base: str) -> StatusResult:
        if knowledge_base != "cooking":
            raise RuntimeError("Cooking adapter received an invalid knowledge base.")
        payload = _json_object(
            self.runner(
                ["status"],
                timeout_seconds=self.status_timeout_seconds,
            )
        )
        return StatusResult(
            healthy=True,
            knowledge_base="cooking",
            documents=int(payload.get("recipes") or 0),
            chunks=int(payload.get("sections") or 0),
            embedding_model=_optional_string(payload.get("embedding_model")),
            embedding_dimensions=_optional_int(payload.get("embedding_dimensions")),
            index_version=COOKING_INDEX_VERSION,
            last_indexed_at=_optional_datetime(payload.get("last_indexed_at")),
        )


def _status_result(
    payload: dict[str, Any],
    knowledge_base: str,
) -> StatusResult:
    return StatusResult(
        healthy=True,
        knowledge_base=knowledge_base,
        documents=int(payload.get("documents") or 0),
        chunks=int(payload.get("chunks") or 0),
        embedding_model=_optional_string(payload.get("embedding_model")),
        embedding_dimensions=_optional_int(payload.get("embedding_dimensions")),
        index_version=_optional_int(payload.get("index_version")),
        last_indexed_at=_optional_string(payload.get("last_indexed_at")),
    )


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_datetime(value: object) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise RuntimeError(
            "Retrieval backend returned an invalid indexing timestamp."
        ) from exc
