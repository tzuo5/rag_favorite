"""Portable document indexing and semantic retrieval core."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from pgvector import Vector

from .config import (
    AppConfig,
    CollectionConfig,
    ConfigError,
    load_config,
)
from .database import connect_database
from .embedding import client_from_config, embedding_space_id, index_model_id
from .indexes import assert_write_snapshot, resolve_index

SUPPORTED_SUFFIXES = frozenset({".md", ".txt"})
MAX_CHUNK_CHARACTERS = 1800
CHUNK_OVERLAP_CHARACTERS = 250
INDEX_VERSION = 3

# Compatibility exports for existing integrations. New code should pass AppConfig.
_DEFAULT_CONFIG = load_config()
KNOWLEDGE_BASES = _DEFAULT_CONFIG.collections
EMBEDDING_MODEL = _DEFAULT_CONFIG.embedding.model
EMBEDDING_DIMENSIONS = _DEFAULT_CONFIG.embedding.dimensions


def get_knowledge_base(key: str, config: AppConfig | None = None) -> CollectionConfig:
    return (config or _DEFAULT_CONFIG).collection(key)


def path_within_root(path: Path, root: Path) -> Path:
    resolved_path = path.expanduser().resolve()
    resolved_root = root.expanduser().resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ConfigError(
            f"Path is outside the selected collection root: {resolved_path}"
        ) from exc
    return resolved_path


def infer_knowledge_base(
    path: Path, config: AppConfig | None = None
) -> CollectionConfig:
    resolved_config = config or _DEFAULT_CONFIG
    resolved_path = path.expanduser().resolve()
    matches = [
        collection
        for collection in resolved_config.collections.values()
        if resolved_path.is_relative_to(collection.path.expanduser().resolve())
    ]
    if not matches:
        raise ConfigError("Source path is not inside a configured collection root.")
    return max(matches, key=lambda item: len(item.path.parts))


def initialize_schema(config: AppConfig | None = None) -> None:
    from .migrations import MigrationRunner

    resolved = config or load_config()
    applied = MigrationRunner(resolved).apply()
    print("RAG database schema initialized: " + ", ".join(applied or ("current",)))


def request_embeddings(
    texts: list[str],
    *,
    query_mode: bool = False,
    config: AppConfig | None = None,
) -> list[list[float]]:
    resolved = resolve_index(config or load_config())
    client = client_from_config(resolved)
    values = (
        [client.embed_query(text) for text in texts]
        if query_mode
        else client.embed_documents(texts)
    )
    return [list(value) for value in values]


def embed_documents(
    chunks: list[str], config: AppConfig | None = None
) -> list[list[float]]:
    resolved = resolve_index(config or load_config())
    client = client_from_config(resolved)
    result: list[list[float]] = []
    batch_size = resolved.embedding.batch_size
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        batch_number = start // batch_size + 1
        total = (len(chunks) + batch_size - 1) // batch_size
        print(f"Embedding batch {batch_number}/{total}...")
        result.extend([list(value) for value in client.embed_documents(batch)])
    return result


def calculate_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def choose_chunk_boundary(text: str, start: int, provisional_end: int) -> int:
    if provisional_end >= len(text):
        return len(text)
    minimum = start + MAX_CHUNK_CHARACTERS // 2
    candidates = [
        text.rfind(marker, minimum, provisional_end)
        for marker in ("\n\n", "\n", "。", ". ", "！", "？")
    ]
    boundary = max(candidates)
    return provisional_end if boundary <= start else boundary + 1


def chunk_text(text: str) -> list[str]:
    normalized = normalize_text(text)
    if not normalized:
        return []
    if len(normalized) <= MAX_CHUNK_CHARACTERS:
        return [normalized]
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        provisional_end = min(start + MAX_CHUNK_CHARACTERS, len(normalized))
        end = choose_chunk_boundary(normalized, start, provisional_end)
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(normalized):
            break
        start = max(end - CHUNK_OVERLAP_CHARACTERS, start + 1)
    return chunks


def find_supported_files(path: Path) -> list[Path]:
    if path.is_file():
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise ConfigError(f"Unsupported file type: {path.suffix}")
        return [path]
    if not path.is_dir():
        raise ConfigError(f"Path does not exist: {path}")
    return sorted(
        item
        for item in path.rglob("*")
        if item.is_file() and item.suffix.lower() in SUPPORTED_SUFFIXES
    )


def document_is_current(
    source_path: str,
    source_sha256: str,
    collection_key: str,
    config: AppConfig | None = None,
) -> bool:
    resolved = resolve_index(config or load_config())
    with connect_database(resolved) as connection:
        row = connection.execute(
            """
            SELECT source_sha256, embedding_model, embedding_dimensions,
                   chunking_version, index_version, knowledge_base
            FROM rag_documents WHERE source_path = %s
            """,
            (source_path,),
        ).fetchone()
    return bool(
        row
        and row[0] == source_sha256
        and row[1] == index_model_id(resolved.embedding)
        and row[2] == resolved.embedding.dimensions
        and row[3] == 1
        and row[4] == INDEX_VERSION
        and row[5] == collection_key
    )


def ensure_embedding_space(config: AppConfig) -> None:
    """Reject wrong encoder metadata before any embedding request."""
    resolved = resolve_index(config)
    with connect_database(resolved) as connection:
        _assert_embedding_space(connection, resolved)


def _assert_embedding_space(connection, config: AppConfig) -> None:
    if config.index.generation != "legacy":
        stored = connection.execute(
            "SELECT to_regclass(%s) IS NOT NULL, to_regclass(%s) IS NOT NULL",
            (
                config.index.schema + ".rag_documents",
                config.index.schema + ".rag_chunks",
            ),
        ).fetchone()
        if not stored or not all(stored):
            raise ConfigError(
                "Generation index tables are missing; refusing legacy schema fallback."
            )
        row = connection.execute(
            "SELECT space_id FROM public.rag_index_generations WHERE id=%s",
            (config.index.generation,),
        ).fetchone()
        if row is None or row[0] != embedding_space_id(config.embedding):
            raise ConfigError("Generation embedding space mismatch.")
    incompatible = connection.execute(
        "SELECT embedding_model FROM rag_documents "
        "WHERE embedding_model <> %s OR embedding_dimensions <> %s LIMIT 1",
        (index_model_id(config.embedding), config.embedding.dimensions),
    ).fetchone()
    if incompatible:
        raise ConfigError(
            "Embedding space mismatch. Create and build a pinned shadow generation before switching encoders. Unversioned legacy vectors cannot be reused safely."
        )


def ingest_file(
    path: Path,
    knowledge_base_key: str | None = None,
    config: AppConfig | None = None,
) -> bool:
    resolved = resolve_index(config or load_config())
    if resolved.unified:
        from .knowledge_migration import import_single_document
        from .video_config import load_video_config

        return import_single_document(
            resolved, load_video_config(resolved.knowledge_profile), path
        )
    collection = (
        resolved.collection(knowledge_base_key)
        if knowledge_base_key
        else infer_knowledge_base(path, resolved)
    )
    if collection.read_only:
        raise ConfigError(f"Collection {collection.key!r} is read-only.")
    source = path_within_root(path, collection.path)
    relative = source.relative_to(collection.path.expanduser().resolve()).as_posix()
    digest = calculate_sha256(source)
    ensure_embedding_space(resolved)
    if document_is_current(str(source), digest, collection.key, resolved):
        print(f"Unchanged; skipped: {relative}")
        return False
    chunks = chunk_text(source.read_text(encoding="utf-8", errors="replace"))
    if not chunks:
        print(f"Empty; skipped: {relative}")
        return False
    print(f"Indexing: {collection.key}/{relative}")
    embeddings = embed_documents(chunks, resolved)
    if calculate_sha256(source) != digest:
        raise ConfigError("Source file changed during embedding; retry the operation.")
    with connect_database(resolved) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(85800874160583809)")
        assert_write_snapshot(connection, resolved)
        _assert_embedding_space(connection, resolved)
        row = connection.execute(
            """
            INSERT INTO rag_documents (
                source_path, knowledge_base, source_relative_path, source_name,
                source_type, source_sha256, embedding_model,
                embedding_dimensions, chunking_version, index_version, indexed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, %s, now())
            ON CONFLICT (source_path) DO UPDATE SET
                knowledge_base = EXCLUDED.knowledge_base,
                source_relative_path = EXCLUDED.source_relative_path,
                source_name = EXCLUDED.source_name,
                source_type = EXCLUDED.source_type,
                source_sha256 = EXCLUDED.source_sha256,
                embedding_model = EXCLUDED.embedding_model,
                embedding_dimensions = EXCLUDED.embedding_dimensions,
                chunking_version = 1,
                index_version = EXCLUDED.index_version,
                indexed_at = now()
            RETURNING id
            """,
            (
                str(source),
                collection.key,
                relative,
                source.name,
                source.suffix.lower(),
                digest,
                index_model_id(resolved.embedding),
                resolved.embedding.dimensions,
                INDEX_VERSION,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("Failed to create document record.")
        document_id = row[0]
        connection.execute(
            "DELETE FROM rag_chunks WHERE document_id = %s", (document_id,)
        )
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO rag_chunks (
                    document_id, chunk_index, content, character_count, embedding
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (document_id, index, chunk, len(chunk), Vector(embedding))
                    for index, (chunk, embedding) in enumerate(
                        zip(chunks, embeddings, strict=True)
                    )
                ],
            )
    print(f"Indexed {len(chunks)} chunk(s): {collection.key}/{relative}")
    return True


def ingest_path(
    path_text: str, collection_key: str, config: AppConfig | None = None
) -> None:
    resolved = resolve_index(config or load_config())
    collection = resolved.collection(collection_key)
    source = path_within_root(Path(path_text), collection.path)
    indexed = 0
    skipped = 0
    for item in find_supported_files(source):
        if ingest_file(item, collection.key, resolved):
            indexed += 1
        else:
            skipped += 1
    print(f"Ingestion complete. Indexed: {indexed}; skipped: {skipped}.")


def search_documents(
    query: str,
    limit: int,
    knowledge_base_keys: list[str],
    config: AppConfig | None = None,
) -> list[dict[str, object]]:
    resolved = resolve_index(config or load_config())
    if resolved.unified:
        from .unified_library import search
        from .video_config import load_video_config

        return search(
            resolved, load_video_config(resolved.knowledge_profile), query, limit
        )
    cleaned = query.strip()
    if not cleaned:
        raise ConfigError("Search query cannot be empty.")
    selected = [resolved.collection(key).key for key in knowledge_base_keys]
    ensure_embedding_space(resolved)
    query_vector = Vector(list(client_from_config(resolved).embed_query(cleaned)))
    with connect_database(resolved) as connection:
        connection.execute("SELECT pg_advisory_xact_lock_shared(85800874160583809)")
        _assert_embedding_space(connection, resolved)
        rows = connection.execute(
            """
            SELECT d.id, d.knowledge_base, d.source_relative_path, d.source_name,
                   d.source_sha256, c.chunk_index, c.content,
                   1 - (c.embedding <=> %s) AS cosine_similarity
            FROM rag_chunks AS c
            JOIN rag_documents AS d ON d.id = c.document_id
            WHERE d.knowledge_base = ANY(%s)
            ORDER BY c.embedding <=> %s LIMIT %s
            """,
            (query_vector, selected, query_vector, max(1, limit)),
        ).fetchall()
    return [
        {
            "result_type": "document",
            "document_id": f"document:{row[0]}",
            "title": row[3],
            "knowledge_base": row[1],
            "source_relative_path": row[2],
            "section": f"chunk:{row[5]}",
            "chunk_index": int(row[5]),
            "excerpt": row[6],
            "semantic_score": float(row[7]),
            "lexical_score": None,
            "combined_score": float(row[7]),
            "reliable": True,
            "content_hash": row[4],
            "metadata": {
                "generation": resolved.index.generation,
                "embedding_space_id": embedding_space_id(resolved.embedding),
            },
        }
        for row in rows
    ]


def semantic_search(
    query: str,
    limit: int,
    knowledge_base_keys: list[str],
    *,
    json_output: bool = False,
    config: AppConfig | None = None,
) -> None:
    results = search_documents(query, limit, knowledge_base_keys, config)
    if json_output:
        print(json.dumps({"results": results}, ensure_ascii=False, default=str))
        return
    if not results:
        print("No indexed content found. Run the ingest command first.")
        return
    for rank, result in enumerate(results, start=1):
        print(f"\nResult #{rank} | similarity={result['semantic_score']:.4f}")
        print(f"File: {result['title']}")
        print(f"Collection: {result['knowledge_base']}")
        print(f"Relative path: {result['source_relative_path']}")
        print(result["excerpt"])


def status_data(
    knowledge_base_key: str | None = None,
    config: AppConfig | None = None,
) -> dict[str, object]:
    resolved = resolve_index(config or load_config())
    if resolved.unified:
        from .unified_library import status
        from .video_config import load_video_config

        result = status(resolved, load_video_config(resolved.knowledge_profile))
        return {
            **result,
            "documents": result["published_documents"],
            "chunks": result["published_chunks"],
            "knowledge_base": "all",
            "embedding_model": resolved.embedding.model,
            "embedding_dimensions": resolved.embedding.dimensions,
            "embedding_backend": resolved.embedding.backend,
            "embedding_space_id": embedding_space_id(resolved.embedding),
            "index_generation": result["generation"],
            "index_version": INDEX_VERSION,
        }
    selected = (
        resolved.collection(knowledge_base_key).key if knowledge_base_key else None
    )
    with connect_database(resolved) as connection:
        rows = connection.execute(
            """
            SELECT d.knowledge_base, COUNT(DISTINCT d.id), COUNT(c.id),
                   MAX(d.indexed_at), MAX(d.index_version)
            FROM rag_documents AS d
            LEFT JOIN rag_chunks AS c ON c.document_id = d.id
            WHERE (%s::text IS NULL OR d.knowledge_base = %s)
            GROUP BY d.knowledge_base ORDER BY d.knowledge_base
            """,
            (selected, selected),
        ).fetchall()
    collections = {
        row[0]: {
            "documents": int(row[1]),
            "chunks": int(row[2]),
            "last_indexed_at": row[3].isoformat() if row[3] else None,
            "index_version": int(row[4]) if row[4] else INDEX_VERSION,
        }
        for row in rows
    }
    last_values = [
        value["last_indexed_at"]
        for value in collections.values()
        if value["last_indexed_at"] is not None
    ]
    return {
        "knowledge_base": selected or "all",
        "documents": sum(value["documents"] for value in collections.values()),
        "chunks": sum(value["chunks"] for value in collections.values()),
        "embedding_model": resolved.embedding.model,
        "embedding_dimensions": resolved.embedding.dimensions,
        "embedding_backend": resolved.embedding.backend,
        "embedding_space_id": embedding_space_id(resolved.embedding),
        "index_generation": resolved.index.generation,
        "index_version": max(
            (value["index_version"] for value in collections.values()),
            default=INDEX_VERSION,
        ),
        "last_indexed_at": max(last_values, default=None),
        "collections": collections,
    }


def show_status(
    knowledge_base_key: str | None = None,
    *,
    json_output: bool = False,
    config: AppConfig | None = None,
) -> None:
    status = status_data(knowledge_base_key, config)
    if json_output:
        print(json.dumps(status, ensure_ascii=False, default=str))
        return
    print(f"Collection: {status['knowledge_base']}")
    print(f"Documents: {status['documents']}")
    print(f"Chunks: {status['chunks']}")
    print(f"Embedding model: {status['embedding_model']}")


def run_doctor(config: AppConfig | None = None) -> None:
    resolved = resolve_index(config or load_config())
    ensure_embedding_space(resolved)
    with connect_database(resolved) as connection:
        vector_version = connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
    if vector_version is None:
        raise RuntimeError("pgvector extension is unavailable.")
    embedding = client_from_config(resolved).embed_document("RAG health check")
    print(f"PostgreSQL pgvector: {vector_version[0]}")
    print(
        f"Embedding provider/model: {resolved.embedding.backend}/{resolved.embedding.model}"
    )
    print(f"Returned dimensions: {len(embedding)}")
    print("RAG dependencies are healthy.")


def add_rag_commands(
    commands: Any,
    config: AppConfig,
) -> None:
    commands.add_parser("init", help="Initialize the database schema.")
    ingest = commands.add_parser("ingest", help="Index a file or directory.")
    ingest.add_argument("path")
    ingest.add_argument(
        "--collection",
        "--knowledge-base",
        dest="collection",
        required=not config.unified,
        default="all" if config.unified else None,
        choices=(
            "all",
            "general",
            "cooking",
            "tech",
            "career",
            "finance",
            "social-conduct",
            "thought-politics",
            "literature-culture",
        )
        if config.unified
        else tuple(config.collections),
    )
    search = commands.add_parser("search", help="Search indexed content.")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=3)
    search.add_argument(
        "--collection",
        "--knowledge-base",
        dest="collections",
        action="append",
        required=not config.unified,
        choices=(
            "all",
            "general",
            "cooking",
            "tech",
            "career",
            "finance",
            "social-conduct",
            "thought-politics",
            "literature-culture",
        )
        if config.unified
        else tuple(config.collections),
    )
    search.add_argument("--json", action="store_true")
    status = commands.add_parser("status", help="Show index statistics.")
    status.add_argument(
        "--collection",
        "--knowledge-base",
        dest="collection",
        choices=tuple(config.collections),
    )
    status.add_argument("--json", action="store_true")
    commands.add_parser("doctor", help="Check PostgreSQL and Ollama.")


def create_parser(config: AppConfig | None = None) -> argparse.ArgumentParser:
    resolved = config or load_config()
    parser = argparse.ArgumentParser(description="Local-first rag-favorite core")
    commands = parser.add_subparsers(dest="command", required=True)
    add_rag_commands(commands, resolved)
    return parser


def run_cli(arguments: argparse.Namespace, config: AppConfig) -> None:
    if arguments.command == "init":
        initialize_schema(config)
    elif arguments.command == "ingest":
        ingest_path(arguments.path, arguments.collection, config)
    elif arguments.command == "search":
        semantic_search(
            arguments.query,
            max(1, arguments.limit),
            arguments.collections,
            json_output=arguments.json,
            config=config,
        )
    elif arguments.command == "status":
        show_status(arguments.collection, json_output=arguments.json, config=config)
    elif arguments.command == "doctor":
        run_doctor(config)
