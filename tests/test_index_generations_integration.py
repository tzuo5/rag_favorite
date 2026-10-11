"""Opt-in real PostgreSQL + local Ollama tests, always in a disposable new DB."""

import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import sql

from rag_favorite.config import (
    CollectionConfig,
    ConfigError,
    IndexConfig,
    database_credentials,
    load_config,
)
from rag_favorite.database import connect_database
from rag_favorite.indexes import (
    activate_generation,
    build_generation,
    create_generation,
    resolve_index,
)
from rag_favorite.migrations import (
    MigrationRunner,
    load_migration,
    migration_checksum,
    render_migration,
)
from rag_favorite.rag import ingest_file, search_documents

PROFILE = os.environ.get("RAG_FAVORITE_INTEGRATION_CONFIG")
pytestmark = pytest.mark.skipif(
    not PROFILE, reason="Needs an explicit real local-model/DB profile"
)


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    profile = load_config(Path(PROFILE))
    assert (
        profile.embedding.backend in {"lmstudio", "ollama"}
        and profile.embedding.model_digest
    )
    assert not any(
        os.environ.get(key)
        for key in ("RAG_DATABASE_NAME", "PGDATABASE", "POSTGRES_DB")
    ), "Unset database-name environment overrides before this isolated test."
    credentials = database_credentials(profile)
    name = "rag_phase1_test_" + uuid4().hex[:12]
    with connect_database(profile, register_pgvector=False) as owner:
        owner.autocommit = True
        owner.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    root = tmp_path_factory.mktemp("phase1-live")
    # Do not let a database name in the profile's secrets override our new DB.
    secrets = root / "secrets.env"
    secrets.touch(mode=0o600)
    secrets.write_text("RAG_DATABASE_PASSWORD=" + credentials["password"] + "\n")
    collections = {}
    for key in ("cooking", "tech"):
        path = root / key
        path.mkdir()
        collections[key] = CollectionConfig(key, key, path)
    (root / "cooking/sauce.md").write_text(
        "凉拌料汁：生抽 2 勺、老抽 1 勺、香醋 1 勺。"
    )
    (root / "tech/database.md").write_text(
        "PostgreSQL 数据备份使用 pg_dump，恢复使用 pg_restore。"
    )
    config = replace(
        profile,
        database=replace(
            profile.database,
            name=name,
            user=credentials["user"],
            credentials_file=secrets,
        ),
        collections=collections,
    )
    try:
        # Exercise the real upgrade path from an existing, checksummed 0001.
        source = load_migration("0001_document_rag.sql")
        with connect_database(config, register_pgvector=False) as connection:
            connection.execute(render_migration(source, config.embedding.dimensions))
            connection.execute(
                "CREATE TABLE rag_favorite_schema_migrations(version text PRIMARY KEY,checksum text NOT NULL,embedding_dimensions integer NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())"
            )
            connection.execute(
                "INSERT INTO rag_favorite_schema_migrations(version,checksum,embedding_dimensions) VALUES ('0001_document_rag.sql',%s,%s)",
                (migration_checksum(source), config.embedding.dimensions),
            )
        assert MigrationRunner(config).apply() == (
            "0002_index_generations.sql",
            "0003_video_knowledge.sql",
        )
        assert MigrationRunner(config).apply() == ()
        with connect_database(config) as connection:
            connection.execute(
                "INSERT INTO public.rag_documents (source_path,knowledge_base,source_relative_path,source_name,source_type,source_sha256,embedding_model,embedding_dimensions) VALUES ('/legacy/note.md','tech','note.md','note.md','.md','legacy','unversioned',1024)"
            )
        create_generation(config, "test-a")
        build_generation(config, "test-a")
        alternate = replace(
            config,
            embedding=replace(
                config.embedding,
                preprocessing_version="qwen-query-v2",
                query_instruction="Instruct: Retrieve evidence answering the question.\nQuery: ",
            ),
        )
        create_generation(alternate, "test-b")
        build_generation(config, "test-b")
        activate_generation(config, "test-a")
        yield config
    finally:
        with connect_database(profile, register_pgvector=False) as owner:
            owner.autocommit = True
            owner.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


def test_real_chinese_queries_collection_isolation_and_legacy_preservation(live):
    rows = search_documents("凉拌料汁需要多少生抽和老抽？", 3, ["cooking"], live)
    assert rows and "生抽 2 勺" in rows[0]["excerpt"]
    assert all(r["knowledge_base"] == "cooking" for r in rows)
    rows = search_documents("怎样备份 PostgreSQL？", 3, ["tech"], live)
    assert rows and "pg_dump" in rows[0]["excerpt"]
    assert all(r["knowledge_base"] == "tech" for r in rows)
    with connect_database(live) as connection:
        assert connection.execute(
            "SELECT embedding_model FROM public.rag_documents"
        ).fetchall() == [("unversioned",)]


def test_atomic_encoder_schema_switch_and_frozen_inflight_snapshot(live):
    frozen = resolve_index(live)
    activate_generation(live, "test-b")
    try:
        current = resolve_index(live)
        assert current.index.generation == "test-b"
        assert current.embedding.preprocessing_version == "qwen-query-v2"
        assert current.index.schema != frozen.index.schema
        assert (
            search_documents("料汁", 1, ["cooking"], frozen)[0]["metadata"][
                "generation"
            ]
            == "test-a"
        )
        assert (
            search_documents("料汁", 1, ["cooking"], live)[0]["metadata"]["generation"]
            == "test-b"
        )
    finally:
        activate_generation(live, "test-a")


def test_same_dimension_wrong_space_rejected_before_embedding_api(live, monkeypatch):
    config = resolve_index(live)
    wrong = replace(
        config,
        embedding=replace(
            config.embedding, query_instruction="wrong same-dimension space"
        ),
    )
    monkeypatch.setattr(
        "rag_favorite.rag.client_from_config",
        lambda *_: pytest.fail("Embedding API must not run for a wrong space"),
    )
    with pytest.raises(ConfigError, match="space mismatch"):
        search_documents("query", 1, ["cooking"], wrong)


def test_incomplete_shadow_and_active_rebuild_rejected(live):
    create_generation(live, "test-empty")
    with pytest.raises(ConfigError, match="not ready"):
        activate_generation(live, "test-empty")
    with pytest.raises(ConfigError, match="active generation"):
        build_generation(live, "test-a")
    with pytest.raises(ConfigError, match="already exists"):
        create_generation(live, "test-a")


def test_stale_snapshot_requires_explicit_rollback_option(live):
    path = live.collection("cooking").path / "sauce.md"
    original = path.read_text()
    path.write_text(original + " 新的人工编辑")
    try:
        with pytest.raises(ConfigError, match="stale"):
            activate_generation(live, "test-b")
        activate_generation(live, "test-b", allow_stale=True)
        assert (
            "新的人工编辑"
            not in search_documents("料汁", 1, ["cooking"], live)[0]["excerpt"]
        )
    finally:
        path.write_text(original)
        activate_generation(live, "test-a")


def test_activation_during_embedding_cannot_commit_to_old_active(live, monkeypatch):
    from rag_favorite import rag

    embed = rag.embed_documents
    path = live.collection("cooking").path / "sauce.md"
    original = path.read_text()
    path.write_text(original + " 第二次导入")

    def switch_after_embedding(texts, config):
        vectors = embed(texts, config)
        activate_generation(live, "test-b", allow_stale=True)
        return vectors

    monkeypatch.setattr(rag, "embed_documents", switch_after_embedding)
    try:
        with pytest.raises(ConfigError, match="changed during indexing"):
            ingest_file(path, "cooking", live)
        snapshot = resolve_index(replace(live, index=IndexConfig("test-a")))
        with connect_database(snapshot) as connection:
            assert connection.execute(
                "SELECT source_sha256 FROM rag_documents WHERE source_path=%s",
                (str(path),),
            ).fetchone()[0] != rag.calculate_sha256(path)
    finally:
        path.write_text(original)
        activate_generation(live, "test-a")


def test_missing_generation_tables_do_not_fall_back_to_public(live):
    create_generation(live, "test-broken")
    config = resolve_index(replace(live, index=IndexConfig("test-broken")))
    with connect_database(config) as connection:
        connection.execute("DROP TABLE rag_chunks")
    with pytest.raises(ConfigError, match="tables are missing"):
        search_documents("query", 1, ["tech"], config)


def test_source_edit_during_embedding_is_not_committed(live, monkeypatch):
    from rag_favorite import rag

    embed = rag.embed_documents
    path = live.collection("cooking").path / "sauce.md"
    original = path.read_text()
    path.write_text(original + " 第一次编辑")

    def edit_during_embedding(texts, config):
        vectors = embed(texts, config)
        path.write_text(original + " 第二次编辑")
        return vectors

    monkeypatch.setattr(rag, "embed_documents", edit_during_embedding)
    try:
        with pytest.raises(ConfigError, match="Source file changed"):
            ingest_file(path, "cooking", live)
    finally:
        path.write_text(original)
