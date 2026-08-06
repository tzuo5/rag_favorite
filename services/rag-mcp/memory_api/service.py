"""Pure preparation layer for governed memory repository calls."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from .contracts import (
    ArchiveMemoryRequest,
    CreateMemoryRequest,
    RestoreMemoryRequest,
    UpdateMemoryRequest,
)
from .policy import (
    build_request_hash,
    canonical_metadata,
    content_sha256,
    derive_initial_status,
    normalize_content,
)


@dataclass(frozen=True, slots=True)
class PreparedCreate:
    memory_id: UUID
    revision_id: UUID
    namespace: str
    memory_type: str
    content: str
    content_hash: str
    source_type: str
    source_ref: str | None
    trust_level: str
    reason: str
    idempotency_key: str
    request_hash: str
    metadata: dict[str, object]
    initial_status: str


@dataclass(frozen=True, slots=True)
class PreparedUpdate:
    memory_id: UUID
    revision_id: UUID
    expected_version: int
    expected_state_version: int
    content: str
    content_hash: str
    source_type: str
    source_ref: str | None
    trust_level: str
    reason: str
    idempotency_key: str
    request_hash: str
    metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class PreparedLifecycle:
    memory_id: UUID
    expected_version: int
    expected_state_version: int
    reason: str
    idempotency_key: str
    request_hash: str
    source_ref: str | None


def prepare_create(request: CreateMemoryRequest) -> PreparedCreate:
    content = normalize_content(request.content)
    content_hash = content_sha256(content)
    metadata = canonical_metadata(request.metadata)

    request_hash = build_request_hash(
        operation="create",
        memory_id=None,
        expected_version=None,
        expected_state_version=None,
        namespace=request.namespace,
        memory_type=request.memory_type,
        normalized_content_hash=content_hash,
        source_type=request.source_type,
        source_ref=request.source_ref,
        trust_level=request.trust_level,
        reason=request.reason,
        metadata=metadata,
    )

    return PreparedCreate(
        memory_id=uuid4(),
        revision_id=uuid4(),
        namespace=request.namespace,
        memory_type=request.memory_type,
        content=content,
        content_hash=content_hash,
        source_type=request.source_type,
        source_ref=request.source_ref,
        trust_level=request.trust_level,
        reason=request.reason,
        idempotency_key=request.idempotency_key,
        request_hash=request_hash,
        metadata=metadata,
        initial_status=derive_initial_status(request),
    )


def prepare_update(request: UpdateMemoryRequest) -> PreparedUpdate:
    content = normalize_content(request.content)
    content_hash = content_sha256(content)
    metadata = canonical_metadata(request.metadata)

    request_hash = build_request_hash(
        operation="update",
        memory_id=str(request.memory_id),
        expected_version=request.expected_version,
        expected_state_version=request.expected_state_version,
        namespace=None,
        memory_type=None,
        normalized_content_hash=content_hash,
        source_type=request.source_type,
        source_ref=request.source_ref,
        trust_level=request.trust_level,
        reason=request.reason,
        metadata=metadata,
    )

    return PreparedUpdate(
        memory_id=request.memory_id,
        revision_id=uuid4(),
        expected_version=request.expected_version,
        expected_state_version=request.expected_state_version,
        content=content,
        content_hash=content_hash,
        source_type=request.source_type,
        source_ref=request.source_ref,
        trust_level=request.trust_level,
        reason=request.reason,
        idempotency_key=request.idempotency_key,
        request_hash=request_hash,
        metadata=metadata,
    )


def prepare_archive(
    request: ArchiveMemoryRequest,
) -> PreparedLifecycle:
    return _prepare_lifecycle("archive", request)


def prepare_restore(
    request: RestoreMemoryRequest,
) -> PreparedLifecycle:
    return _prepare_lifecycle("restore", request)


def _prepare_lifecycle(
    operation: str,
    request: ArchiveMemoryRequest | RestoreMemoryRequest,
) -> PreparedLifecycle:
    request_hash = build_request_hash(
        operation=operation,
        memory_id=str(request.memory_id),
        expected_version=request.expected_version,
        expected_state_version=request.expected_state_version,
        namespace=None,
        memory_type=None,
        normalized_content_hash=None,
        source_type=None,
        source_ref=request.source_ref,
        trust_level=None,
        reason=request.reason,
        metadata=None,
    )

    return PreparedLifecycle(
        memory_id=request.memory_id,
        expected_version=request.expected_version,
        expected_state_version=request.expected_state_version,
        reason=request.reason,
        idempotency_key=request.idempotency_key,
        request_hash=request_hash,
        source_ref=request.source_ref,
    )

# BEGIN PHASE 5.2 APPLICATION SERVICE

from collections.abc import Callable
from typing import Any, Protocol
from uuid import uuid4

from pydantic import ValidationError

from .contracts import MutationResult
from .errors import DomainError


class MemoryRepositoryProtocol(Protocol):
    def create(
        self,
        prepared: PreparedCreate,
    ) -> dict[str, Any]:
        ...

    def update(
        self,
        prepared: PreparedUpdate,
    ) -> dict[str, Any]:
        ...

    def archive(
        self,
        prepared: PreparedLifecycle,
    ) -> dict[str, Any]:
        ...

    def restore(
        self,
        prepared: PreparedLifecycle,
    ) -> dict[str, Any]:
        ...


class MemoryService:
    """Orchestrate validation, preparation, repository calls and output."""

    def __init__(
        self,
        repository: MemoryRepositoryProtocol,
        *,
        request_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._repository = repository
        self._request_id_factory = request_id_factory

    def create(
        self,
        request: CreateMemoryRequest,
    ) -> MutationResult:
        prepared = prepare_create(request)
        row = self._repository.create(prepared)

        return self._build_result(
            expected_operation="create",
            row=row,
        )

    def update(
        self,
        request: UpdateMemoryRequest,
    ) -> MutationResult:
        prepared = prepare_update(request)
        row = self._repository.update(prepared)

        return self._build_result(
            expected_operation="update",
            row=row,
        )

    def archive(
        self,
        request: ArchiveMemoryRequest,
    ) -> MutationResult:
        prepared = prepare_archive(request)
        row = self._repository.archive(prepared)

        return self._build_result(
            expected_operation="archive",
            row=row,
        )

    def restore(
        self,
        request: RestoreMemoryRequest,
    ) -> MutationResult:
        prepared = prepare_restore(request)
        row = self._repository.restore(prepared)

        return self._build_result(
            expected_operation="restore",
            row=row,
        )

    def _build_result(
        self,
        *,
        expected_operation: str,
        row: dict[str, Any],
    ) -> MutationResult:
        if row.get("operation") != expected_operation:
            raise DomainError("INTERNAL_ERROR")

        payload = {
            "ok": True,
            **row,
            "request_id": self._request_id_factory(),
        }

        try:
            return MutationResult.model_validate(payload)
        except ValidationError:
            raise DomainError("INTERNAL_ERROR") from None


# END PHASE 5.2 APPLICATION SERVICE
