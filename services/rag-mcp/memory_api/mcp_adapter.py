from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from memory_api.contracts import (
    ArchiveMemoryRequest,
    CreateMemoryRequest,
    MutationResult,
    RestoreMemoryRequest,
    UpdateMemoryRequest,
)
from memory_api.errors import DomainError
from memory_api.service import MemoryService


def _new_request_id() -> str:
    return str(uuid4())


def _handle_mutation(
    *,
    payload: dict[str, Any],
    service: MemoryService,
    request_model: type[BaseModel],
    service_method: str,
) -> dict[str, object]:
    error_request_id = _new_request_id()

    try:
        request = request_model.model_validate(payload)
        method = getattr(service, service_method)
        result = method(request)

        if not isinstance(result, MutationResult):
            raise DomainError("INTERNAL_ERROR")

        return result.model_dump(mode="json")

    except ValidationError:
        return DomainError("VALIDATION_ERROR").to_dict(error_request_id)

    except DomainError as error:
        return error.to_dict(error_request_id)

    except Exception:
        return DomainError("INTERNAL_ERROR").to_dict(error_request_id)


def handle_memory_create(
    payload: dict[str, Any],
    service: MemoryService,
) -> dict[str, object]:
    return _handle_mutation(
        payload=payload,
        service=service,
        request_model=CreateMemoryRequest,
        service_method="create",
    )


def handle_memory_update(
    payload: dict[str, Any],
    service: MemoryService,
) -> dict[str, object]:
    return _handle_mutation(
        payload=payload,
        service=service,
        request_model=UpdateMemoryRequest,
        service_method="update",
    )


def handle_memory_archive(
    payload: dict[str, Any],
    service: MemoryService,
) -> dict[str, object]:
    return _handle_mutation(
        payload=payload,
        service=service,
        request_model=ArchiveMemoryRequest,
        service_method="archive",
    )


def handle_memory_restore(
    payload: dict[str, Any],
    service: MemoryService,
) -> dict[str, object]:
    return _handle_mutation(
        payload=payload,
        service=service,
        request_model=RestoreMemoryRequest,
        service_method="restore",
    )
