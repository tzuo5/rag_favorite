from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from memory_api.mcp_adapter import (
    handle_memory_archive,
    handle_memory_create,
    handle_memory_restore,
    handle_memory_update,
)
from memory_api.service import MemoryService


MUTATION_TOOL_NAMES = (
    "rag_memory_create",
    "rag_memory_update",
    "rag_memory_archive",
    "rag_memory_restore",
)


def register_memory_mutation_tools(
    mcp: FastMCP,
    service: MemoryService,
) -> tuple[str, ...]:
    def rag_memory_create(
        namespace: str,
        memory_type: str,
        content: str,
        source_type: str,
        trust_level: str,
        reason: str,
        idempotency_key: str,
        source_ref: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        return handle_memory_create(
            {
                "namespace": namespace,
                "memory_type": memory_type,
                "content": content,
                "source_type": source_type,
                "source_ref": source_ref,
                "trust_level": trust_level,
                "reason": reason,
                "idempotency_key": idempotency_key,
                "metadata": metadata or {},
            },
            service,
        )

    def rag_memory_update(
        memory_id: str,
        expected_version: int,
        expected_state_version: int,
        content: str,
        source_type: str,
        trust_level: str,
        reason: str,
        idempotency_key: str,
        source_ref: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        return handle_memory_update(
            {
                "memory_id": memory_id,
                "expected_version": expected_version,
                "expected_state_version": expected_state_version,
                "content": content,
                "source_type": source_type,
                "source_ref": source_ref,
                "trust_level": trust_level,
                "reason": reason,
                "idempotency_key": idempotency_key,
                "metadata": metadata or {},
            },
            service,
        )

    def rag_memory_archive(
        memory_id: str,
        expected_version: int,
        expected_state_version: int,
        reason: str,
        idempotency_key: str,
        source_ref: str | None = None,
    ) -> dict[str, object]:
        return handle_memory_archive(
            {
                "memory_id": memory_id,
                "expected_version": expected_version,
                "expected_state_version": expected_state_version,
                "reason": reason,
                "idempotency_key": idempotency_key,
                "source_ref": source_ref,
            },
            service,
        )

    def rag_memory_restore(
        memory_id: str,
        expected_version: int,
        expected_state_version: int,
        reason: str,
        idempotency_key: str,
        source_ref: str | None = None,
    ) -> dict[str, object]:
        return handle_memory_restore(
            {
                "memory_id": memory_id,
                "expected_version": expected_version,
                "expected_state_version": expected_state_version,
                "reason": reason,
                "idempotency_key": idempotency_key,
                "source_ref": source_ref,
            },
            service,
        )

    registrations = (
        (
            rag_memory_create,
            "rag_memory_create",
            "Create a governed long-term memory record.",
        ),
        (
            rag_memory_update,
            "rag_memory_update",
            "Create a new revision of an existing governed memory.",
        ),
        (
            rag_memory_archive,
            "rag_memory_archive",
            "Soft-archive an existing governed memory.",
        ),
        (
            rag_memory_restore,
            "rag_memory_restore",
            "Restore a previously archived governed memory.",
        ),
    )

    for function, name, description in registrations:
        mcp.add_tool(
            function,
            name=name,
            description=description,
            structured_output=True,
        )

    return MUTATION_TOOL_NAMES
