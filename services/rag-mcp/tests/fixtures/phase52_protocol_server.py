from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from mcp.server.fastmcp import FastMCP

from memory_api.contracts import MutationResult
from memory_api.fastmcp_registration import register_memory_mutation_tools


class ProtocolFixtureService:
    def create(self, request: object) -> MutationResult:
        return MutationResult.model_validate(
            {
                "ok": True,
                "operation": "create",
                "memory_id": UUID(
                    "11111111-1111-4111-8111-111111111111"
                ),
                "revision_id": UUID(
                    "22222222-2222-4222-8222-222222222222"
                ),
                "event_id": 1,
                "current_version": 1,
                "state_version": 1,
                "status": "active",
                "content_hash": "a" * 64,
                "replayed": False,
                "request_id": UUID(
                    "33333333-3333-4333-8333-333333333333"
                ),
            }
        )

    def update(self, request: object) -> MutationResult:
        raise RuntimeError("fixture update was not expected")

    def archive(self, request: object) -> MutationResult:
        raise RuntimeError("fixture archive was not expected")

    def restore(self, request: object) -> MutationResult:
        raise RuntimeError("fixture restore was not expected")


server = FastMCP("Phase 5.2 isolated protocol fixture")

register_memory_mutation_tools(
    server,
    ProtocolFixtureService(),
)

if __name__ == "__main__":
    server.run(transport="stdio")
