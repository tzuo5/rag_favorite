#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

import psycopg
from dotenv import dotenv_values
from pgvector import Vector
from pgvector.psycopg import register_vector

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

DB_ENV_PATH = Path.home() / "services/rag-postgres/.env"

OLLAMA_EMBED_URL = "http://127.0.0.1:11434/api/embed"
EMBEDDING_MODEL = "qwen3-embedding:0.6b"
EMBEDDING_DIMENSIONS = 1024

SUPPORTED_SUFFIXES = {".md", ".txt"}

# Character-based chunking is intentionally used for the first version.
# It handles mixed Chinese and English without installing a tokenizer.
MAX_CHUNK_CHARACTERS = 1800
CHUNK_OVERLAP_CHARACTERS = 250

# Keep this small because the server is running Qwen on CPU.
EMBEDDING_BATCH_SIZE = 4

QUERY_INSTRUCTION = (
    "Instruct: Given a user question, retrieve relevant passages "
    "from a personal knowledge base.\n"
    "Query: "
)

INDEX_VERSION = 2


class KnowledgeBase(NamedTuple):
    key: str
    display_name: str
    root: Path


KNOWLEDGE_BASES = {
    item.key: item
    for item in (
        KnowledgeBase("thought-politics", "Thought and Politics", Path("/home/ubuntu/知识库/Thought and Politics")),
        KnowledgeBase("tech", "Technology", Path("/home/ubuntu/知识库/Technology")),
        KnowledgeBase("finance", "Finance and Investment", Path("/home/ubuntu/知识库/Finance and Investment")),
        KnowledgeBase("career", "Career Development", Path("/home/ubuntu/知识库/Career Development")),
        KnowledgeBase("social-conduct", "Chinese Social Relations and Conduct", Path("/home/ubuntu/知识库/Chinese Social Relations and Conduct")),
        KnowledgeBase("literature-culture", "Literature and Culture", Path("/home/ubuntu/知识库/Literature and Culture")),
        KnowledgeBase("general", "General Resources", Path("/home/ubuntu/知识库/General Resources")),
        KnowledgeBase("legacy", "Legacy Archive (read-only migration source)", Path("/home/ubuntu/知识库/Legacy Archive")),
    )
}


def get_knowledge_base(key: str) -> KnowledgeBase:
    try:
        return KNOWLEDGE_BASES[key]
    except KeyError as error:
        choices = ", ".join(KNOWLEDGE_BASES)
        raise RuntimeError(
            f"Unknown knowledge base {key!r}; choose one of: {choices}"
        ) from error


def path_within_root(path: Path, root: Path) -> Path:
    resolved_path = path.expanduser().resolve()
    resolved_root = root.expanduser().resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as error:
        raise RuntimeError(
            f"Path is outside the selected knowledge-base root: {resolved_path}"
        ) from error
    return resolved_path


def infer_knowledge_base(path: Path) -> KnowledgeBase:
    resolved_path = path.expanduser().resolve()
    matches = [
        knowledge_base
        for knowledge_base in KNOWLEDGE_BASES.values()
        if resolved_path.is_relative_to(knowledge_base.root.resolve())
    ]
    if not matches:
        raise RuntimeError(
            "Source path is not inside a configured knowledge-base root."
        )
    return max(matches, key=lambda item: len(item.root.parts))


# ---------------------------------------------------------
# Database
# ---------------------------------------------------------

def load_database_settings() -> dict[str, str]:
    raw_settings = dotenv_values(DB_ENV_PATH)

    required_names = (
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    )

    missing = [
        name
        for name in required_names
        if not raw_settings.get(name)
    ]

    if missing:
        missing_text = ", ".join(missing)
        raise RuntimeError(
            f"Missing settings in {DB_ENV_PATH}: {missing_text}"
        )

    return {
        name: str(raw_settings[name])
        for name in required_names
    }


def connect_database() -> psycopg.Connection:
    settings = load_database_settings()

    connection = psycopg.connect(
        host="127.0.0.1",
        port=5432,
        dbname=settings["POSTGRES_DB"],
        user=settings["POSTGRES_USER"],
        password=settings["POSTGRES_PASSWORD"],
        connect_timeout=10,
    )

    register_vector(connection)

    return connection


def initialize_schema() -> None:
    settings = load_database_settings()

    # The first connection does not register pgvector yet, because the
    # extension must exist before its PostgreSQL types can be registered.
    with psycopg.connect(
        host="127.0.0.1",
        port=5432,
        dbname=settings["POSTGRES_DB"],
        user=settings["POSTGRES_USER"],
        password=settings["POSTGRES_PASSWORD"],
        connect_timeout=10,
    ) as connection:
        connection.execute(
            "CREATE EXTENSION IF NOT EXISTS vector"
        )

        register_vector(connection)

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rag_documents (
                id bigserial PRIMARY KEY,
                source_path text NOT NULL UNIQUE,
                knowledge_base text NOT NULL,
                source_relative_path text NOT NULL,
                source_name text NOT NULL,
                source_type text NOT NULL,
                source_sha256 text NOT NULL,
                embedding_model text NOT NULL,
                embedding_dimensions integer NOT NULL,
                chunking_version integer NOT NULL DEFAULT 1,
                index_version integer NOT NULL DEFAULT 2,
                indexed_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )

        connection.execute(
            """
            ALTER TABLE rag_documents
                ADD COLUMN IF NOT EXISTS knowledge_base text
                    NOT NULL DEFAULT 'legacy',
                ADD COLUMN IF NOT EXISTS source_relative_path text
                    NOT NULL DEFAULT '',
                ADD COLUMN IF NOT EXISTS index_version integer
                    NOT NULL DEFAULT 1
            """
        )

        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS rag_chunks (
                id bigserial PRIMARY KEY,

                document_id bigint NOT NULL
                    REFERENCES rag_documents(id)
                    ON DELETE CASCADE,

                chunk_index integer NOT NULL,
                content text NOT NULL,
                character_count integer NOT NULL,

                embedding vector({EMBEDDING_DIMENSIONS}) NOT NULL,

                created_at timestamptz NOT NULL DEFAULT now(),

                UNIQUE (document_id, chunk_index)
            )
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS
                rag_chunks_document_id_idx
            ON rag_chunks(document_id)
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS
                rag_documents_knowledge_base_idx
            ON rag_documents(knowledge_base)
            """
        )

    print("RAG database schema initialized.")
    print(f"Embedding model: {EMBEDDING_MODEL}")
    print(f"Embedding dimensions: {EMBEDDING_DIMENSIONS}")


# ---------------------------------------------------------
# Ollama Embeddings
# ---------------------------------------------------------

def request_embeddings(
    texts: list[str],
    *,
    query_mode: bool = False,
) -> list[list[float]]:
    if not texts:
        return []

    prepared_texts = texts

    if query_mode:
        prepared_texts = [
            QUERY_INSTRUCTION + text
            for text in texts
        ]

    request_body = json.dumps(
        {
            "model": EMBEDDING_MODEL,
            "input": prepared_texts,
        },
        ensure_ascii=False,
    ).encode("utf-8")

    request = urllib.request.Request(
        OLLAMA_EMBED_URL,
        data=request_body,
        headers={
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=600,
        ) as response:
            result = json.load(response)

    except urllib.error.URLError as error:
        raise RuntimeError(
            "Unable to reach Ollama at "
            f"{OLLAMA_EMBED_URL}. "
            "Check that the Ollama container is running."
        ) from error

    embeddings = result.get("embeddings")

    if not isinstance(embeddings, list):
        raise RuntimeError(
            "Ollama response does not contain embeddings."
        )

    if len(embeddings) != len(texts):
        raise RuntimeError(
            "Embedding count mismatch: "
            f"requested {len(texts)}, "
            f"received {len(embeddings)}."
        )

    for index, embedding in enumerate(embeddings):
        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise RuntimeError(
                f"Embedding #{index} has {len(embedding)} "
                f"dimensions; expected {EMBEDDING_DIMENSIONS}."
            )

    return embeddings


def embed_documents(
    chunks: list[str],
) -> list[list[float]]:
    all_embeddings: list[list[float]] = []

    for start in range(
        0,
        len(chunks),
        EMBEDDING_BATCH_SIZE,
    ):
        batch = chunks[
            start:start + EMBEDDING_BATCH_SIZE
        ]

        batch_number = (
            start // EMBEDDING_BATCH_SIZE
        ) + 1

        total_batches = (
            len(chunks)
            + EMBEDDING_BATCH_SIZE
            - 1
        ) // EMBEDDING_BATCH_SIZE

        print(
            f"Embedding batch "
            f"{batch_number}/{total_batches}..."
        )

        all_embeddings.extend(
            request_embeddings(batch)
        )

    return all_embeddings


# ---------------------------------------------------------
# Document processing
# ---------------------------------------------------------

def calculate_sha256(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for block in iter(
            lambda: file.read(1024 * 1024),
            b"",
        ):
            digest.update(block)

    return digest.hexdigest()


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")

    lines = [
        line.rstrip()
        for line in text.splitlines()
    ]

    return "\n".join(lines).strip()


def choose_chunk_boundary(
    text: str,
    start: int,
    provisional_end: int,
) -> int:
    if provisional_end >= len(text):
        return len(text)

    minimum_boundary = (
        start + MAX_CHUNK_CHARACTERS // 2
    )

    candidates = [
        text.rfind(
            "\n\n",
            minimum_boundary,
            provisional_end,
        ),
        text.rfind(
            "\n",
            minimum_boundary,
            provisional_end,
        ),
        text.rfind(
            "。",
            minimum_boundary,
            provisional_end,
        ),
        text.rfind(
            ". ",
            minimum_boundary,
            provisional_end,
        ),
        text.rfind(
            "！",
            minimum_boundary,
            provisional_end,
        ),
        text.rfind(
            "？",
            minimum_boundary,
            provisional_end,
        ),
    ]

    best_boundary = max(candidates)

    if best_boundary <= start:
        return provisional_end

    return best_boundary + 1


def chunk_text(text: str) -> list[str]:
    normalized = normalize_text(text)

    if not normalized:
        return []

    if len(normalized) <= MAX_CHUNK_CHARACTERS:
        return [normalized]

    chunks: list[str] = []
    start = 0

    while start < len(normalized):
        provisional_end = min(
            start + MAX_CHUNK_CHARACTERS,
            len(normalized),
        )

        end = choose_chunk_boundary(
            normalized,
            start,
            provisional_end,
        )

        chunk = normalized[start:end].strip()

        if chunk:
            chunks.append(chunk)

        if end >= len(normalized):
            break

        next_start = max(
            end - CHUNK_OVERLAP_CHARACTERS,
            start + 1,
        )

        start = next_start

    return chunks


def find_supported_files(path: Path) -> list[Path]:
    if path.is_file():
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise RuntimeError(
                f"Unsupported file type: {path.suffix}"
            )

        return [path]

    if not path.is_dir():
        raise RuntimeError(
            f"Path does not exist: {path}"
        )

    return sorted(
        file
        for file in path.rglob("*")
        if (
            file.is_file()
            and file.suffix.lower()
            in SUPPORTED_SUFFIXES
        )
    )


def document_is_current(
    source_path: str,
    source_sha256: str,
    knowledge_base: str,
) -> bool:
    with connect_database() as connection:
        row = connection.execute(
            """
            SELECT
                source_sha256,
                embedding_model,
                embedding_dimensions,
                chunking_version,
                index_version,
                knowledge_base
            FROM rag_documents
            WHERE source_path = %s
            """,
            (source_path,),
        ).fetchone()

    if row is None:
        return False

    (
        stored_sha256,
        stored_model,
        stored_dimensions,
        stored_chunking_version,
        stored_index_version,
        stored_knowledge_base,
    ) = row

    return (
        stored_sha256 == source_sha256
        and stored_model == EMBEDDING_MODEL
        and stored_dimensions == EMBEDDING_DIMENSIONS
        and stored_chunking_version == 1
        and stored_index_version == INDEX_VERSION
        and stored_knowledge_base == knowledge_base
    )


def ingest_file(
    path: Path,
    knowledge_base_key: str | None = None,
) -> bool:
    knowledge_base = (
        get_knowledge_base(knowledge_base_key)
        if knowledge_base_key
        else infer_knowledge_base(path)
    )
    resolved_path = path_within_root(path, knowledge_base.root)
    source_path = str(resolved_path)
    source_relative_path = resolved_path.relative_to(
        knowledge_base.root.resolve()
    ).as_posix()
    source_sha256 = calculate_sha256(resolved_path)

    if document_is_current(
        source_path,
        source_sha256,
        knowledge_base.key,
    ):
        print(f"Unchanged; skipped: {source_path}")
        return False

    text = resolved_path.read_text(
        encoding="utf-8",
        errors="replace",
    )

    chunks = chunk_text(text)

    if not chunks:
        print(f"Empty; skipped: {source_path}")
        return False

    print()
    print(f"Indexing: {source_path}")
    print(f"Generated chunks: {len(chunks)}")

    embeddings = embed_documents(chunks)

    with connect_database() as connection:
        row = connection.execute(
            """
            INSERT INTO rag_documents (
                source_path,
                knowledge_base,
                source_relative_path,
                source_name,
                source_type,
                source_sha256,
                embedding_model,
                embedding_dimensions,
                chunking_version,
                index_version,
                indexed_at
            )
            VALUES (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                1,
                %s,
                now()
            )
            ON CONFLICT (source_path)
            DO UPDATE SET
                source_name =
                    EXCLUDED.source_name,
                knowledge_base =
                    EXCLUDED.knowledge_base,
                source_relative_path =
                    EXCLUDED.source_relative_path,
                source_type =
                    EXCLUDED.source_type,
                source_sha256 =
                    EXCLUDED.source_sha256,
                embedding_model =
                    EXCLUDED.embedding_model,
                embedding_dimensions =
                    EXCLUDED.embedding_dimensions,
                chunking_version = 1,
                index_version =
                    EXCLUDED.index_version,
                indexed_at = now()
            RETURNING id
            """,
            (
                source_path,
                knowledge_base.key,
                source_relative_path,
                resolved_path.name,
                resolved_path.suffix.lower(),
                source_sha256,
                EMBEDDING_MODEL,
                EMBEDDING_DIMENSIONS,
                INDEX_VERSION,
            ),
        ).fetchone()

        if row is None:
            raise RuntimeError(
                "Failed to create document record."
            )

        document_id = row[0]

        connection.execute(
            """
            DELETE FROM rag_chunks
            WHERE document_id = %s
            """,
            (document_id,),
        )

        records = [
            (
                document_id,
                chunk_index,
                chunk,
                len(chunk),
                Vector(embedding),
            )
            for chunk_index, (
                chunk,
                embedding,
            ) in enumerate(
                zip(
                    chunks,
                    embeddings,
                    strict=True,
                )
            )
        ]

        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO rag_chunks (
                    document_id,
                    chunk_index,
                    content,
                    character_count,
                    embedding
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                records,
            )

    print(
        f"Indexed {len(chunks)} chunk(s): "
        f"{source_path}"
    )

    return True


def ingest_path(path_text: str, knowledge_base_key: str) -> None:
    knowledge_base = get_knowledge_base(knowledge_base_key)
    path = path_within_root(Path(path_text), knowledge_base.root)
    files = find_supported_files(path)

    if not files:
        print("No supported Markdown or text files found.")
        return

    indexed_count = 0
    skipped_count = 0

    for file in files:
        if ingest_file(file, knowledge_base.key):
            indexed_count += 1
        else:
            skipped_count += 1

    print()
    print("Ingestion complete.")
    print(f"Indexed files: {indexed_count}")
    print(f"Skipped files: {skipped_count}")


# ---------------------------------------------------------
# Search
# ---------------------------------------------------------

def semantic_search(
    query: str,
    limit: int,
    knowledge_base_keys: list[str],
    *,
    json_output: bool = False,
) -> None:
    results = search_documents(query, limit, knowledge_base_keys)

    if json_output:
        print(
            json.dumps(
                {"results": results},
                ensure_ascii=False,
                default=str,
            )
        )
        return

    if not results:
        print(
            "No indexed content found. "
            "Run the ingest command first."
        )
        return

    for rank, result in enumerate(results, start=1):
        print()
        print("=" * 78)
        print(
            f"Result #{rank} | "
            f"similarity={result['semantic_score']:.4f}"
        )
        print(f"File: {result['title']}")
        print(f"Knowledge base: {result['knowledge_base']}")
        print(f"Relative path: {result['source_relative_path']}")
        print(f"Chunk: {result['chunk_index']}")
        print("-" * 78)
        print(result["excerpt"])


def search_documents(
    query: str,
    limit: int,
    knowledge_base_keys: list[str],
) -> list[dict[str, object]]:
    """Return safe structured search results without absolute source paths."""

    cleaned_query = query.strip()

    if not cleaned_query:
        raise RuntimeError(
            "Search query cannot be empty."
        )

    query_embedding = request_embeddings(
        [cleaned_query],
        query_mode=True,
    )[0]

    query_vector = Vector(query_embedding)
    selected = [
        get_knowledge_base(key).key
        for key in knowledge_base_keys
    ]

    with connect_database() as connection:
        rows = connection.execute(
            """
            SELECT
                d.id,
                d.knowledge_base,
                d.source_relative_path,
                d.source_name,
                d.source_sha256,
                c.chunk_index,
                c.content,
                1 - (c.embedding <=> %s)
                    AS cosine_similarity
            FROM rag_chunks AS c
            JOIN rag_documents AS d
                ON d.id = c.document_id
            WHERE d.knowledge_base = ANY(%s)
            ORDER BY c.embedding <=> %s
            LIMIT %s
            """,
            (
                query_vector,
                selected,
                query_vector,
                limit,
            ),
        ).fetchall()

    results: list[dict[str, object]] = []
    for row in rows:
        (
            document_id,
            knowledge_base,
            source_relative_path,
            source_name,
            source_sha256,
            chunk_index,
            content,
            similarity,
        ) = row
        semantic_score = float(similarity)
        results.append(
            {
                "result_type": "document",
                "document_id": f"topic:{document_id}",
                "title": source_name,
                "knowledge_base": knowledge_base,
                "source_relative_path": source_relative_path,
                "section": f"chunk:{chunk_index}",
                "chunk_index": int(chunk_index),
                "excerpt": content,
                "semantic_score": semantic_score,
                "lexical_score": None,
                "combined_score": semantic_score,
                "reliable": True,
                "content_hash": source_sha256,
                "metadata": {},
            }
        )
    return results


# ---------------------------------------------------------
# Status and health checks
# ---------------------------------------------------------

def status_data(knowledge_base_key: str | None = None) -> dict[str, object]:
    selected = (
        get_knowledge_base(knowledge_base_key).key
        if knowledge_base_key
        else None
    )
    with connect_database() as connection:
        rows = connection.execute(
            """
            SELECT
                d.knowledge_base,
                COUNT(DISTINCT d.id),
                COUNT(c.id),
                MAX(d.indexed_at),
                MAX(d.index_version)
            FROM rag_documents AS d
            LEFT JOIN rag_chunks AS c
                ON c.document_id = d.id
            WHERE (%s::text IS NULL OR d.knowledge_base = %s)
            GROUP BY d.knowledge_base
            ORDER BY d.knowledge_base
            """
            ,
            (selected, selected),
        ).fetchall()

    collections = {
        key: {
            "documents": int(document_count),
            "chunks": int(chunk_count),
            "last_indexed_at": (
                last_indexed_at.isoformat()
                if last_indexed_at is not None
                else None
            ),
            "index_version": (
                int(index_version)
                if index_version is not None
                else INDEX_VERSION
            ),
        }
        for (
            key,
            document_count,
            chunk_count,
            last_indexed_at,
            index_version,
        ) in rows
    }
    last_indexed_values = [
        value["last_indexed_at"]
        for value in collections.values()
        if value["last_indexed_at"] is not None
    ]
    return {
        "knowledge_base": selected or "all",
        "documents": sum(value["documents"] for value in collections.values()),
        "chunks": sum(value["chunks"] for value in collections.values()),
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dimensions": EMBEDDING_DIMENSIONS,
        "index_version": max(
            (
                value["index_version"]
                for value in collections.values()
            ),
            default=INDEX_VERSION,
        ),
        "last_indexed_at": max(last_indexed_values, default=None),
        "collections": collections,
    }


def show_status(
    knowledge_base_key: str | None = None,
    *,
    json_output: bool = False,
) -> None:
    status = status_data(knowledge_base_key)
    if json_output:
        print(json.dumps(status, ensure_ascii=False, default=str))
        return

    print(f"Knowledge base: {status['knowledge_base']}")
    print(f"Documents: {status['documents']}")
    print(f"Chunks: {status['chunks']}")
    for key, collection in status["collections"].items():
        print(
            f"Collection {key}: {collection['documents']} documents, "
            f"{collection['chunks']} chunks"
        )
    print(f"Embedding model: {status['embedding_model']}")
    print(
        f"Embedding dimensions: "
        f"{status['embedding_dimensions']}"
    )


def run_doctor() -> None:
    print("Checking PostgreSQL...")

    with connect_database() as connection:
        vector_version = connection.execute(
            """
            SELECT extversion
            FROM pg_extension
            WHERE extname = 'vector'
            """
        ).fetchone()

    if vector_version is None:
        raise RuntimeError(
            "pgvector extension is unavailable."
        )

    print(
        f"PostgreSQL pgvector: "
        f"{vector_version[0]}"
    )

    print("Checking Ollama...")

    embedding = request_embeddings(
        ["RAG health check"]
    )[0]

    print(f"Ollama model: {EMBEDDING_MODEL}")
    print(
        f"Returned dimensions: "
        f"{len(embedding)}"
    )

    print("RAG dependencies are healthy.")


# ---------------------------------------------------------
# CLI
# ---------------------------------------------------------

def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Local RAG system using Qwen3 Embedding, "
            "Ollama, PostgreSQL, and pgvector."
        )
    )

    commands = parser.add_subparsers(
        dest="command",
        required=True,
    )

    commands.add_parser(
        "init",
        help="Initialize the database schema.",
    )

    ingest_parser = commands.add_parser(
        "ingest",
        help="Index a Markdown/text file or directory.",
    )
    ingest_parser.add_argument(
        "path",
        help="File or directory to index.",
    )
    ingest_parser.add_argument(
        "--knowledge-base",
        required=True,
        choices=tuple(KNOWLEDGE_BASES),
        help="Isolated destination knowledge base.",
    )

    search_parser = commands.add_parser(
        "search",
        help="Search indexed content semantically.",
    )
    search_parser.add_argument(
        "query",
        help="Natural-language search query.",
    )
    search_parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help="Maximum number of results.",
    )
    search_parser.add_argument(
        "--knowledge-base",
        action="append",
        required=True,
        choices=tuple(KNOWLEDGE_BASES),
        help=(
            "Knowledge base to search. Repeat only for an explicit "
            "cross-library search."
        ),
    )
    search_parser.add_argument(
        "--json",
        action="store_true",
        help="Return a safe structured JSON result.",
    )

    status_parser = commands.add_parser(
        "status",
        help="Show index statistics.",
    )
    status_parser.add_argument(
        "--knowledge-base",
        choices=tuple(KNOWLEDGE_BASES),
        help="Show one isolated knowledge base; omit for a full status report.",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Return a safe structured JSON result.",
    )

    commands.add_parser(
        "doctor",
        help="Check PostgreSQL and Ollama.",
    )

    return parser


def main() -> None:
    parser = create_parser()
    arguments = parser.parse_args()

    try:
        if arguments.command == "init":
            initialize_schema()

        elif arguments.command == "ingest":
            ingest_path(
                arguments.path,
                arguments.knowledge_base,
            )

        elif arguments.command == "search":
            semantic_search(
                arguments.query,
                max(1, arguments.limit),
                arguments.knowledge_base,
                json_output=arguments.json,
            )

        elif arguments.command == "status":
            show_status(
                arguments.knowledge_base,
                json_output=arguments.json,
            )

        elif arguments.command == "doctor":
            run_doctor()

    except KeyboardInterrupt:
        print(
            "\nOperation cancelled.",
            file=sys.stderr,
        )
        raise SystemExit(130)

    except Exception as error:
        print(
            f"Error: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
