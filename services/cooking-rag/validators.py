from __future__ import annotations


MAX_QUERY_CHARACTERS = 1_000


def normalize_query(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("query must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError("query cannot be empty")
    if "\x00" in normalized or len(normalized) > MAX_QUERY_CHARACTERS:
        raise ValueError("query is invalid or too long")
    return normalized
