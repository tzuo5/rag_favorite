from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pgvector import Vector

from database import connect_database
from embedding import OllamaEmbeddingClient
from recipe_documents import RecipeDocument, discover_markdown_files, load_recipe_document


ConnectionFactory = Callable[[], Any]
INDEX_VERSION = 3


class RecipeIndexer:
    def __init__(
        self,
        vault: Path,
        embedding_client: OllamaEmbeddingClient,
        connection_factory: ConnectionFactory = connect_database,
        batch_size: int = 8,
    ) -> None:
        self.vault = vault.expanduser().resolve()
        self.embedding_client = embedding_client
        self.connection_factory = connection_factory
        self.batch_size = batch_size

    def index(self, *, prune: bool = False) -> dict[str, int]:
        files = discover_markdown_files(self.vault)
        documents = [load_recipe_document(self.vault, path) for path in files]
        indexed = 0
        skipped = 0

        for document in documents:
            if not self.index_document(document):
                skipped += 1
                continue
            indexed += 1

        pruned = self._prune({item.source_path for item in documents}) if prune else 0
        return {
            "discovered": len(documents),
            "indexed": indexed,
            "skipped": skipped,
            "pruned": pruned,
        }

    def index_document(self, document: RecipeDocument) -> bool:
        if self._is_current(document):
            return False
        self._replace_document(document)
        return True

    def _is_current(self, document: RecipeDocument) -> bool:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT source_sha256, index_version, embedding_model
                FROM cooking_recipes
                WHERE source_path = %s
                """,
                (document.source_path,),
            ).fetchone()
        return row is not None and (
            row[0] == document.source_sha256
            and row[1] == INDEX_VERSION
            and row[2] == self.embedding_client.model
        )

    def _replace_document(self, document: RecipeDocument) -> None:
        embedding_inputs = [
            f"{document.title}\n{section.heading_path}\n{section.content}"
            for section in document.sections
        ]
        embeddings: list[tuple[float, ...]] = []
        for start in range(0, len(embedding_inputs), self.batch_size):
            embeddings.extend(
                self.embedding_client.embed_documents(
                    embedding_inputs[start : start + self.batch_size]
                )
            )

        with self.connection_factory() as connection:
            row = connection.execute(
                """
                INSERT INTO cooking_recipes (
                    source_path, source_sha256, title, cuisine, category,
                    document_type, content, image_refs, index_version,
                    embedding_model, indexed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (source_path) DO UPDATE SET
                    source_sha256 = EXCLUDED.source_sha256,
                    title = EXCLUDED.title,
                    cuisine = EXCLUDED.cuisine,
                    category = EXCLUDED.category,
                    document_type = EXCLUDED.document_type,
                    content = EXCLUDED.content,
                    image_refs = EXCLUDED.image_refs,
                    index_version = EXCLUDED.index_version,
                    embedding_model = EXCLUDED.embedding_model,
                    indexed_at = now()
                RETURNING id
                """,
                (
                    document.source_path,
                    document.source_sha256,
                    document.title,
                    document.cuisine,
                    document.category,
                    document.document_type,
                    document.content,
                    Jsonb(list(document.image_refs)),
                    INDEX_VERSION,
                    self.embedding_client.model,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("recipe upsert returned no id")
            recipe_id = row[0]
            connection.execute(
                "DELETE FROM cooking_recipe_sections WHERE recipe_id = %s",
                (recipe_id,),
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO cooking_recipe_sections (
                        recipe_id, section_index, heading_path, content, embedding
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            recipe_id,
                            section.section_index,
                            section.heading_path,
                            section.content,
                            Vector(embedding),
                        )
                        for section, embedding in zip(
                            document.sections, embeddings, strict=True
                        )
                    ],
                )

    def _prune(self, current_paths: set[str]) -> int:
        with self.connection_factory() as connection:
            rows = connection.execute("SELECT id, source_path FROM cooking_recipes").fetchall()
            stale_ids = [row[0] for row in rows if row[1] not in current_paths]
            if stale_ids:
                connection.execute(
                    "DELETE FROM cooking_recipes WHERE id = ANY(%s)",
                    (stale_ids,),
                )
        return len(stale_ids)


class RecipeRetriever:
    def __init__(
        self,
        embedding_client: OllamaEmbeddingClient,
        connection_factory: ConnectionFactory = connect_database,
    ) -> None:
        self.embedding_client = embedding_client
        self.connection_factory = connection_factory

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        vector = self.embedding_client.embed_query(query)
        candidate_limit = min(limit * 3, 10)
        with self.connection_factory() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    "SELECT * FROM cooking_api_search(%s, %s::vector, %s)",
                    (query, _vector_text(vector), candidate_limit),
                )
                rows = cursor.fetchall()
        results = [_safe_search_result(dict(row)) for row in rows]
        unique: list[dict[str, Any]] = []
        seen_recipe_ids: set[int] = set()
        for result in results:
            recipe_id = result["recipe_id"]
            if recipe_id in seen_recipe_ids:
                continue
            seen_recipe_ids.add(recipe_id)
            unique.append(result)
            if len(unique) >= limit:
                break
        return unique

    def get(self, recipe_id: int) -> dict[str, Any] | None:
        with self.connection_factory() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute("SELECT * FROM cooking_api_get(%s)", (recipe_id,))
                row = cursor.fetchone()
        return _safe_recipe_result(dict(row)) if row else None

    def status(self) -> dict[str, Any]:
        with self.connection_factory() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute("SELECT * FROM cooking_api_status()")
                row = cursor.fetchone()
        return dict(row) if row else {}


def _vector_text(values: tuple[float, ...]) -> str:
    return "[" + ",".join(format(value, ".17g") for value in values) + "]"


def _safe_search_result(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "recipe_id": int(row["recipe_id"]),
        "title": row.get("title"),
        "cuisine": row.get("cuisine"),
        "category": row.get("category"),
        "document_type": row.get("document_type"),
        "heading_path": row.get("heading_path"),
        "excerpt": row.get("excerpt"),
        "source_path": row.get("source_path"),
        "cosine_similarity": float(row["cosine_similarity"]),
        "lexical_similarity": float(row["lexical_similarity"]),
        "hybrid_score": float(row["hybrid_score"]),
        "reliable": bool(row["reliable"]),
    }


def _safe_recipe_result(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "recipe_id": int(row["recipe_id"]),
        "title": row.get("title"),
        "cuisine": row.get("cuisine"),
        "category": row.get("category"),
        "document_type": row.get("document_type"),
        "content": row.get("content"),
        "content_truncated": bool(row.get("content_truncated")),
        "source_path": row.get("source_path"),
        "image_refs": row.get("image_refs"),
        "indexed_at": row.get("indexed_at").isoformat() if row.get("indexed_at") else None,
    }
