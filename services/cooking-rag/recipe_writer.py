from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row

from recipe_documents import load_recipe_document, normalize_markdown
from repository import RecipeIndexer


CUISINE_DIRECTORIES = {
    "western": "1. Western（西餐）",
    "chinese": "2. Chinese（中餐）",
    "japanese": "3. Japanese（日餐）",
    "alcohol": "4. Alcohol（酒精）",
    "other": "5. Other（其他）",
}

CATEGORY_DIRECTORIES = {
    "appetizer": "前菜",
    "main": "主菜",
    "soup": "汤",
    "dessert_baking": "西点&烘焙",
    "side_component": "配菜&组件",
    "drink": "饮品",
    "other": "其他",
}

MAX_TITLE_CHARACTERS = 120
MAX_RECIPE_BYTES = 30_000
SAFE_TITLE_RE = re.compile(r"^[^/\\\x00-\x1f\x7f]+$")


class RecipeCreateError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable


class RecipeWriter:
    def __init__(self, vault: Path, indexer: RecipeIndexer) -> None:
        self.vault = vault.expanduser().resolve()
        self.indexer = indexer

    def create(
        self,
        *,
        title: str,
        cuisine: str,
        category: str,
        recipe_markdown: str,
    ) -> dict[str, Any]:
        normalized_title = normalize_title(title)
        relative_path = build_relative_path(normalized_title, cuisine, category)
        markdown = canonical_recipe_markdown(normalized_title, recipe_markdown)
        request_hash = recipe_request_hash(
            normalized_title, cuisine, category, markdown
        )

        replay = self._event_by_hash(request_hash)
        if replay is not None:
            return _event_result(replay, replayed=True)

        target = (self.vault / relative_path).resolve()
        if not target.is_relative_to(self.vault):
            raise RecipeCreateError("INVALID_PATH", "Recipe path is invalid.")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        created_file = False
        encoded = markdown.encode("utf-8")
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file():
                raise RecipeCreateError("PATH_CONFLICT", "Recipe path is unavailable.")
            existing = target.read_bytes()
            if existing != encoded:
                raise RecipeCreateError(
                    "RECIPE_EXISTS",
                    "A different recipe already exists with this title.",
                )
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(target, flags, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                created_file = True
            except FileExistsError as exc:
                raise RecipeCreateError(
                    "RECIPE_EXISTS",
                    "A recipe already exists with this title.",
                ) from exc

        try:
            document = load_recipe_document(self.vault, target)
            self.indexer.index_document(document)
            event = self._record_event(
                request_hash=request_hash,
                source_path=relative_path.as_posix(),
                title=normalized_title,
            )
        except RecipeCreateError:
            raise
        except Exception as exc:
            raise RecipeCreateError(
                "INDEXING_FAILED",
                "Recipe file was saved but indexing did not complete; retry the exact request.",
                retryable=True,
            ) from exc

        result = _event_result(event, replayed=False)
        result["file_created"] = created_file
        return result

    def _event_by_hash(self, request_hash: str) -> dict[str, Any] | None:
        with self.indexer.connection_factory() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    """
                    SELECT e.request_hash, e.source_path, e.title, e.recipe_id
                    FROM cooking_recipe_events AS e
                    WHERE e.request_hash = %s
                    """,
                    (request_hash,),
                )
                row = cursor.fetchone()
        return dict(row) if row else None

    def _record_event(
        self,
        *,
        request_hash: str,
        source_path: str,
        title: str,
    ) -> dict[str, Any]:
        with self.indexer.connection_factory() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    "SELECT id FROM cooking_recipes WHERE source_path = %s",
                    (source_path,),
                )
                recipe = cursor.fetchone()
                if recipe is None:
                    raise RecipeCreateError(
                        "INDEXING_FAILED",
                        "Recipe index record is missing.",
                        retryable=True,
                    )
                cursor.execute(
                    """
                    INSERT INTO cooking_recipe_events (
                        request_hash, operation, source_path, title, recipe_id
                    ) VALUES (%s, 'create', %s, %s, %s)
                    ON CONFLICT (request_hash) DO NOTHING
                    RETURNING request_hash, source_path, title, recipe_id
                    """,
                    (request_hash, source_path, title, recipe["id"]),
                )
                event = cursor.fetchone()
                if event is None:
                    cursor.execute(
                        """
                        SELECT request_hash, source_path, title, recipe_id
                        FROM cooking_recipe_events
                        WHERE request_hash = %s
                        """,
                        (request_hash,),
                    )
                    event = cursor.fetchone()
        if event is None:
            raise RecipeCreateError(
                "EVENT_FAILED", "Recipe audit event was not recorded.", retryable=True
            )
        return dict(event)


def normalize_title(value: str) -> str:
    if not isinstance(value, str):
        raise RecipeCreateError("INVALID_TITLE", "Recipe title must be text.")
    title = unicodedata.normalize("NFKC", value).strip().rstrip(".")
    if (
        not title
        or len(title) > MAX_TITLE_CHARACTERS
        or title in {".", ".."}
        or title.startswith(".")
        or not SAFE_TITLE_RE.fullmatch(title)
    ):
        raise RecipeCreateError("INVALID_TITLE", "Recipe title is invalid.")
    return title


def build_relative_path(title: str, cuisine: str, category: str) -> Path:
    cuisine_directory = CUISINE_DIRECTORIES.get(cuisine)
    category_directory = CATEGORY_DIRECTORIES.get(category)
    if cuisine_directory is None:
        raise RecipeCreateError("INVALID_CUISINE", "Cuisine is unsupported.")
    if category_directory is None:
        raise RecipeCreateError("INVALID_CATEGORY", "Recipe category is unsupported.")
    if cuisine == "alcohol":
        return Path(cuisine_directory) / f"{title}.md"
    return Path(cuisine_directory) / category_directory / f"{title}.md"


def canonical_recipe_markdown(title: str, value: str) -> str:
    if not isinstance(value, str):
        raise RecipeCreateError("INVALID_RECIPE", "Recipe content must be text.")
    normalized = normalize_markdown(value)
    lines = normalized.splitlines()
    if lines and re.match(r"^#\s+", lines[0]):
        lines = lines[1:]
    body = "\n".join(lines).strip()
    markdown = f"# {title}\n\n{body}\n"
    if len(body) < 20 or len(markdown.encode("utf-8")) > MAX_RECIPE_BYTES:
        raise RecipeCreateError(
            "INVALID_RECIPE", "Recipe content is incomplete or too large."
        )
    return markdown


def recipe_request_hash(
    title: str, cuisine: str, category: str, markdown: str
) -> str:
    payload = json.dumps(
        {
            "title": title,
            "cuisine": cuisine,
            "category": category,
            "markdown": markdown,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _event_result(event: dict[str, Any], *, replayed: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "operation": "create",
        "recipe_id": int(event["recipe_id"]),
        "title": event["title"],
        "source_path": event["source_path"],
        "indexed": True,
        "replayed": replayed,
    }
