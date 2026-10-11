"""Immutable encoder snapshots and isolated PostgreSQL index generations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

from psycopg import sql
from psycopg.types.json import Jsonb

from .config import (
    AppConfig,
    ConfigError,
    EmbeddingConfig,
    IndexConfig,
    validate_config,
)
from .database import connect_database
from .embedding import client_from_config, embedding_space_id, index_model_id
from .migrations import MigrationRunner, load_migration, render_migration

INDEX_LOCK = 85800874160583809


def _roots(config: AppConfig) -> dict[str, str]:
    return {
        key: str(c.path.expanduser().resolve()) for key, c in config.collections.items()
    }


def _registry_exists(connection) -> bool:
    row = connection.execute(
        "SELECT to_regclass('public.rag_index_generations') IS NOT NULL"
    ).fetchone()
    return bool(row and row[0] is True)


def _record(connection, name: str):
    row = connection.execute(
        "SELECT id, schema_name, space_id, embedding_settings, collection_roots, state "
        "FROM public.rag_index_generations WHERE id = %s",
        (name,),
    ).fetchone()
    if row is None:
        raise ConfigError(f"Unknown index generation: {name}")
    return row


def _snapshot(config: AppConfig, row, *, follow_active: bool = False) -> AppConfig:
    if not config.unified and row[4] != _roots(config):
        raise ConfigError(
            "Generation collection roots differ from the current configuration."
        )
    try:
        embedding = EmbeddingConfig(**row[3])
    except (TypeError, ValueError) as exc:
        raise ConfigError("Generation encoder configuration is invalid.") from exc
    if embedding_space_id(embedding) != row[2]:
        raise ConfigError("Generation embedding space metadata is inconsistent.")
    resolved = replace(
        config,
        embedding=embedding,
        index=IndexConfig(row[0], row[1], True, follow_active),
    )
    validate_config(resolved)
    return resolved


def resolve_index(config: AppConfig) -> AppConfig:
    """Freeze encoder and schema together for the lifetime of one operation."""
    if config.index.resolved:
        return config
    with connect_database(config, register_pgvector=False) as connection:
        exists = _registry_exists(connection)
        name = config.index.generation
        follow_active = name == "active"
        if exists and follow_active:
            row = connection.execute(
                "SELECT active_generation FROM public.rag_index_state WHERE singleton"
            ).fetchone()
            name = row[0] if row else "legacy"
        if not exists or name == "legacy":
            if name not in {"active", "legacy"}:
                raise ConfigError(
                    "Index generation registry is not initialized. Run setup apply."
                )
            return replace(
                config, index=IndexConfig("legacy", "public", True, follow_active)
            )
        return _snapshot(config, _record(connection, name), follow_active=follow_active)


def assert_write_snapshot(connection, config: AppConfig) -> None:
    if not config.index.follow_active:
        return
    if not _registry_exists(connection):
        return
    row = connection.execute(
        "SELECT active_generation FROM public.rag_index_state WHERE singleton"
    ).fetchone()
    active = row[0] if row else "legacy"
    if active != config.index.generation:
        raise ConfigError(
            "Active generation changed during indexing; retry the operation."
        )


def create_generation(config: AppConfig, name: str) -> dict[str, object]:
    config = replace(config, index=IndexConfig())
    if name in {"active", "legacy"}:
        raise ConfigError("active and legacy are reserved generation names.")
    validate_config(replace(config, index=IndexConfig(name)))
    client = client_from_config(config)
    # Health check also verifies pinned local weights and output dimensions.
    client.embed_document("索引空间验证")
    # New generations may have a different dimension from the preserved public schema.
    with connect_database(config, register_pgvector=False) as connection:
        ledger = connection.execute(
            "SELECT to_regclass('public.rag_favorite_schema_migrations')"
        ).fetchone()
        old = (
            connection.execute(
                "SELECT embedding_dimensions FROM public.rag_favorite_schema_migrations "
                "WHERE version = '0001_document_rag.sql'"
            ).fetchone()
            if ledger and ledger[0]
            else None
        )
    baseline = (
        replace(config, embedding=replace(config.embedding, dimensions=old[0]))
        if old
        else config
    )
    MigrationRunner(baseline).apply()
    schema = "rag_gen_" + hashlib.sha256(name.encode()).hexdigest()[:24]
    with connect_database(config) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (INDEX_LOCK,))
        if connection.execute(
            "SELECT 1 FROM public.rag_index_generations WHERE id = %s", (name,)
        ).fetchone():
            raise ConfigError(
                "Generation already exists; create a new name instead of overwriting."
            )
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection.execute(
            sql.SQL("SET LOCAL search_path TO {}, public").format(
                sql.Identifier(schema)
            )
        )
        connection.execute(
            render_migration(
                load_migration("0001_document_rag.sql"), config.embedding.dimensions
            )
        )
        connection.execute(
            "INSERT INTO public.rag_index_generations "
            "(id, schema_name, space_id, embedding_settings, collection_roots) VALUES (%s,%s,%s,%s,%s)",
            (
                name,
                schema,
                embedding_space_id(config.embedding),
                Jsonb(asdict(config.embedding)),
                Jsonb(_roots(config)),
            ),
        )
    return {
        "generation": name,
        "space_id": embedding_space_id(config.embedding),
        "state": "shadow",
    }


def _sources(config: AppConfig) -> list[dict[str, str]]:
    from .rag import (
        calculate_sha256,
        chunk_text,
        find_supported_files,
        infer_knowledge_base,
        path_within_root,
    )

    result = []
    for key, collection in config.collections.items():
        if not collection.path.is_dir():
            raise ConfigError(f"Collection root does not exist: {key}")
        for path in find_supported_files(collection.path):
            path = path_within_root(path, collection.path)
            if infer_knowledge_base(path, config).key != key:
                continue
            if chunk_text(path.read_text(encoding="utf-8", errors="replace")):
                result.append(
                    {
                        "path": str(path),
                        "collection": key,
                        "sha256": calculate_sha256(path),
                    }
                )
    return sorted(result, key=lambda item: (item["collection"], item["path"]))


def _indexed_sources(connection, config: AppConfig) -> list[dict[str, str]]:
    rows = connection.execute(
        "SELECT d.source_path, d.knowledge_base, d.source_sha256, d.embedding_model, "
        "d.embedding_dimensions, COUNT(c.id) FROM rag_documents d "
        "LEFT JOIN rag_chunks c ON c.document_id=d.id GROUP BY d.id"
    ).fetchall()
    for row in rows:
        if (
            row[3] != index_model_id(config.embedding)
            or row[4] != config.embedding.dimensions
            or row[5] == 0
        ):
            raise ConfigError(
                "Generation contains incomplete documents or a mismatched embedding space."
            )
    return sorted(
        [{"path": r[0], "collection": r[1], "sha256": r[2]} for r in rows],
        key=lambda item: (item["collection"], item["path"]),
    )


def build_generation(config: AppConfig, name: str) -> dict[str, object]:
    from .rag import ingest_file

    resolved = resolve_index(replace(config, index=IndexConfig(name)))
    if resolved.index.generation == "legacy":
        raise ConfigError(
            "Build a shadow generation instead of overwriting the legacy index."
        )
    with connect_database(resolved) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (INDEX_LOCK,))
        active = connection.execute(
            "SELECT active_generation FROM public.rag_index_state WHERE singleton"
        ).fetchone()
        if active and active[0] == name:
            raise ConfigError(
                "Cannot rebuild the active generation; create a new shadow generation."
            )
        connection.execute(
            "UPDATE public.rag_index_generations SET state='shadow', ready_at=NULL WHERE id=%s",
            (name,),
        )
    before = _sources(resolved)
    with connect_database(resolved) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (INDEX_LOCK,))
        connection.execute(
            "DELETE FROM rag_documents WHERE NOT (source_path=ANY(%s))",
            ([item["path"] for item in before],),
        )
    for item in before:
        ingest_file(Path(item["path"]), item["collection"], resolved)
    after = _sources(resolved)
    if before != after:
        raise ConfigError(
            "Source files changed during generation build; retry before activation."
        )
    with connect_database(resolved) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (INDEX_LOCK,))
        row = _record(connection, name)
        _snapshot(config, row)
        if row[2] != embedding_space_id(resolved.embedding):
            raise ConfigError("Generation encoder changed during build.")
        indexed = _indexed_sources(connection, resolved)
        if not indexed or indexed != before:
            raise ConfigError(
                "Shadow generation is empty or does not cover the configured source files."
            )
        digest = hashlib.sha256(
            json.dumps(indexed, sort_keys=True).encode()
        ).hexdigest()
        connection.execute(
            "UPDATE public.rag_index_generations SET state='ready', manifest_sha256=%s, "
            "document_count=%s, ready_at=now() WHERE id=%s",
            (digest, len(indexed), name),
        )
    return {
        "generation": name,
        "state": "ready",
        "documents": len(indexed),
        "manifest_sha256": digest,
    }


def activate_generation(
    config: AppConfig, name: str, *, allow_stale: bool = False
) -> dict[str, object]:
    resolved = resolve_index(replace(config, index=IndexConfig(name)))
    if resolved.index.generation == "legacy":
        raise ConfigError(
            "Unversioned legacy indexes cannot be safely activated; rebuild a pinned generation."
        )
    client_from_config(resolved).embed_query("索引空间验证")
    sources = None if allow_stale else _sources(resolved)
    with connect_database(resolved) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (INDEX_LOCK,))
        row = _record(connection, name)
        _snapshot(config, row)
        if row[2] != embedding_space_id(resolved.embedding):
            raise ConfigError("Generation encoder changed during activation.")
        if row[5] != "ready":
            raise ConfigError(
                "Generation is not ready; run index build before activation."
            )
        indexed = _indexed_sources(connection, resolved)
        if not indexed or (sources is not None and indexed != sources):
            raise ConfigError(
                "Generation is incomplete or stale; rebuild, or explicitly roll back with --allow-stale."
            )
        previous = connection.execute(
            "SELECT active_generation FROM public.rag_index_state WHERE singleton"
        ).fetchone()
        connection.execute(
            "INSERT INTO public.rag_index_state(singleton,active_generation) VALUES (true,%s) "
            "ON CONFLICT(singleton) DO UPDATE SET active_generation=EXCLUDED.active_generation,activated_at=now()",
            (name,),
        )
    return {
        "active_generation": name,
        "previous_generation": previous[0] if previous else None,
        "space_id": embedding_space_id(resolved.embedding),
        "allow_stale": allow_stale,
    }


def list_generations(config: AppConfig) -> list[dict[str, object]]:
    with connect_database(config, register_pgvector=False) as connection:
        if not _registry_exists(connection):
            return []
        rows = connection.execute(
            "SELECT g.id,g.space_id,g.state,g.document_count,g.embedding_settings, "
            "COALESCE(g.id=s.active_generation,false) FROM public.rag_index_generations g "
            "LEFT JOIN public.rag_index_state s ON s.singleton ORDER BY g.created_at,g.id"
        ).fetchall()
    return [
        {
            "generation": r[0],
            "space_id": r[1],
            "state": r[2],
            "documents_at_build": r[3],
            "model": r[4]["model"],
            "backend": r[4]["backend"],
            "dimensions": r[4]["dimensions"],
            "active": r[5],
        }
        for r in rows
    ]
