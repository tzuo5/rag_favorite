"""Normalization, trust policy, and request fingerprinting."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .contracts import CreateMemoryRequest


def normalize_content(value: str) -> str:
    """Return the canonical content persisted and hashed by the service."""

    normalized = value.replace("\r\n", "\n").replace("\r", "\n")

    if normalized.startswith("\ufeff"):
        normalized = normalized[1:]

    return normalized.strip()


def content_sha256(content: str) -> str:
    normalized = normalize_content(content)

    if not normalized:
        raise ValueError("normalized content must not be blank")

    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def derive_initial_status(request: CreateMemoryRequest) -> str:
    if (
        request.source_type == "user_explicit"
        and request.trust_level == "high"
        and request.namespace != "quarantine"
    ):
        return "active"

    return "candidate"


def canonical_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(
        json.dumps(
            dict(metadata),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def build_request_hash(
    *,
    operation: str,
    memory_id: str | None,
    expected_version: int | None,
    expected_state_version: int | None,
    namespace: str | None,
    memory_type: str | None,
    normalized_content_hash: str | None,
    source_type: str | None,
    source_ref: str | None,
    trust_level: str | None,
    reason: str,
    metadata: Mapping[str, Any] | None,
) -> str:
    """Build a deterministic fingerprint without storing plaintext content."""

    payload = {
        "operation": operation,
        "memory_id": memory_id,
        "expected_version": expected_version,
        "expected_state_version": expected_state_version,
        "namespace": namespace,
        "memory_type": memory_type,
        "content_hash": normalized_content_hash,
        "source_type": source_type,
        "source_ref": source_ref,
        "trust_level": trust_level,
        "reason": reason.strip(),
        "metadata": canonical_metadata(metadata or {}),
    }

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()
