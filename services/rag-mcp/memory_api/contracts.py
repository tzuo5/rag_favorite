"""Pydantic contracts for governed memory tools."""

from __future__ import annotations

import json
import re
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


Namespace = Literal[
    "personal_memory",
    "project_memory",
    "agent_observations",
    "quarantine",
]

MemoryType = Literal[
    "fact",
    "preference",
    "decision",
    "plan",
    "constraint",
    "project_state",
    "procedure",
    "observation",
    "reference",
]

SourceType = Literal[
    "user_explicit",
    "user_implicit",
    "agent_inference",
    "external_document",
    "tool_output",
    "system_generated",
]

TrustLevel = Literal[
    "untrusted",
    "low",
    "medium",
    "high",
]

MemoryStatus = Literal[
    "candidate",
    "active",
    "archived",
]

Operation = Literal[
    "create",
    "update",
    "archive",
    "restore",
]

IDEMPOTENCY_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,254}$"
)

ALLOWED_METADATA_KEYS = {
    "tags",
    "project",
    "conversation_ref",
    "source_title",
    "language",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
    )


class RevisionInput(StrictModel):
    content: str
    source_type: SourceType
    source_ref: str | None = None
    trust_level: TrustLevel
    reason: str
    idempotency_key: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")

        if len(value.encode("utf-8")) > 32768:
            raise ValueError("content exceeds 32768 UTF-8 bytes")

        return value

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        normalized = value.strip()

        if not normalized:
            raise ValueError("reason must not be blank")

        if len(normalized.encode("utf-8")) > 1024:
            raise ValueError("reason exceeds 1024 UTF-8 bytes")

        return normalized

    @field_validator("source_ref")
    @classmethod
    def validate_source_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None

        normalized = value.strip()

        if not normalized:
            raise ValueError("source_ref must not be blank")

        if len(normalized.encode("utf-8")) > 2048:
            raise ValueError("source_ref exceeds 2048 UTF-8 bytes")

        return normalized

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        if not IDEMPOTENCY_PATTERN.fullmatch(value):
            raise ValueError("idempotency_key format is invalid")

        return value

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        unknown = set(value) - ALLOWED_METADATA_KEYS

        if unknown:
            raise ValueError(
                "metadata contains unsupported keys: "
                + ", ".join(sorted(unknown))
            )

        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

        if len(serialized.encode("utf-8")) > 4096:
            raise ValueError("metadata exceeds 4096 UTF-8 bytes")

        tags = value.get("tags")

        if tags is not None:
            if not isinstance(tags, list):
                raise ValueError("metadata.tags must be an array")

            if len(tags) > 20:
                raise ValueError("metadata.tags may contain at most 20 values")

            if any(
                not isinstance(tag, str)
                or not 1 <= len(tag) <= 64
                for tag in tags
            ):
                raise ValueError(
                    "metadata.tags entries must be strings of length 1..64"
                )

            if len(set(tags)) != len(tags):
                raise ValueError("metadata.tags entries must be unique")

        limits = {
            "project": (1, 128),
            "conversation_ref": (1, 256),
            "source_title": (1, 256),
            "language": (2, 16),
        }

        for key, (minimum, maximum) in limits.items():
            item = value.get(key)

            if item is None:
                continue

            if (
                not isinstance(item, str)
                or not minimum <= len(item) <= maximum
            ):
                raise ValueError(
                    f"metadata.{key} length must be {minimum}..{maximum}"
                )

        return value


class CreateMemoryRequest(RevisionInput):
    namespace: Namespace
    memory_type: MemoryType

    @model_validator(mode="after")
    def validate_policy(self) -> "CreateMemoryRequest":
        _validate_trust_policy(
            namespace=self.namespace,
            source_type=self.source_type,
            trust_level=self.trust_level,
        )
        return self


class UpdateMemoryRequest(RevisionInput):
    memory_id: UUID
    expected_version: int = Field(ge=1)
    expected_state_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_policy(self) -> "UpdateMemoryRequest":
        if (
            self.source_type != "user_explicit"
            and self.trust_level == "high"
        ):
            raise ValueError(
                "high trust requires source_type=user_explicit"
            )

        return self


class LifecycleRequest(StrictModel):
    memory_id: UUID
    expected_version: int = Field(ge=1)
    expected_state_version: int = Field(ge=1)
    reason: str
    idempotency_key: str
    source_ref: str | None = None

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        normalized = value.strip()

        if not normalized:
            raise ValueError("reason must not be blank")

        if len(normalized.encode("utf-8")) > 1024:
            raise ValueError("reason exceeds 1024 UTF-8 bytes")

        return normalized

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        if not IDEMPOTENCY_PATTERN.fullmatch(value):
            raise ValueError("idempotency_key format is invalid")

        return value

    @field_validator("source_ref")
    @classmethod
    def validate_source_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None

        normalized = value.strip()

        if not normalized:
            raise ValueError("source_ref must not be blank")

        if len(normalized.encode("utf-8")) > 2048:
            raise ValueError("source_ref exceeds 2048 UTF-8 bytes")

        return normalized


class ArchiveMemoryRequest(LifecycleRequest):
    pass


class RestoreMemoryRequest(LifecycleRequest):
    pass


class MutationResult(StrictModel):
    ok: Literal[True] = True
    operation: Operation
    memory_id: UUID
    revision_id: UUID | None
    event_id: int = Field(ge=1)
    current_version: int = Field(ge=1)
    state_version: int = Field(ge=1)
    status: MemoryStatus
    content_hash: str | None
    replayed: bool
    request_id: UUID

    @field_validator("content_hash")
    @classmethod
    def validate_content_hash(cls, value: str | None) -> str | None:
        if value is None:
            return None

        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("content_hash must be lowercase SHA-256")

        return value


def _validate_trust_policy(
    *,
    namespace: Namespace,
    source_type: SourceType,
    trust_level: TrustLevel,
) -> None:
    if trust_level == "high" and source_type != "user_explicit":
        raise ValueError("high trust requires source_type=user_explicit")

    if namespace == "agent_observations" and trust_level == "high":
        raise ValueError("agent_observations cannot be high trust")

    if namespace == "quarantine" and trust_level != "untrusted":
        raise ValueError("quarantine requires trust_level=untrusted")
