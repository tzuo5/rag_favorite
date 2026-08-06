"""Stable domain errors for Agent-facing memory operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


ERROR_MESSAGES: Final[dict[str, str]] = {
    "VALIDATION_ERROR": "The request is invalid.",
    "NOT_FOUND": "The memory was not found.",
    "CONCURRENCY_CONFLICT": "The memory changed after it was read.",
    "INVALID_STATE": "The memory is not in a valid state for this operation.",
    "ALREADY_ARCHIVED": "The memory is already archived.",
    "NOT_ARCHIVED": "The memory is not archived.",
    "IDEMPOTENCY_CONFLICT": "The idempotency key was reused for another request.",
    "PERMISSION_DENIED": "This operation is not permitted.",
    "TIMEOUT": "The operation timed out.",
    "EMBEDDING_UNAVAILABLE": "The memory embedding service is unavailable.",
    "DATABASE_UNAVAILABLE": "The memory database is unavailable.",
    "INTERNAL_ERROR": "The operation failed.",
}


@dataclass(slots=True)
class DomainError(Exception):
    """Error safe to map into the stable Agent-facing envelope."""

    code: str
    field: str | None = None
    retryable: bool = False
    safe_message: str | None = None

    def __post_init__(self) -> None:
        if self.code not in ERROR_MESSAGES:
            raise ValueError(f"Unknown domain error code: {self.code}")

        Exception.__init__(self, self.message)

    @property
    def message(self) -> str:
        return self.safe_message or ERROR_MESSAGES[self.code]

    def to_dict(self, request_id: str) -> dict[str, object]:
        error: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }

        if self.field is not None:
            error["field"] = self.field

        return {
            "ok": False,
            "error": error,
            "request_id": request_id,
        }
