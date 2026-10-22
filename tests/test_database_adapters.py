"""Real adapters and disposable databases; never call a model or alter production."""

import gc
import os
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from pgvector import Bit, HalfVector, SparseVector, Vector
from psycopg import sql
from psycopg.adapt import Loader
from psycopg.pq import TransactionStatus

from rag_favorite import database
from rag_favorite.config import IndexConfig, database_credentials, load_config


@pytest.fixture
def databases(tmp_path):
    profile = os.environ.get("RAG_DATABASE_TEST_CONFIG")
    if not profile:
        pytest.skip("Set RAG_DATABASE_TEST_CONFIG to create disposable databases")
    base = load_config(profile)
    credentials = database_credentials(base)
    assert not any(
        os.environ.get(k) for k in ("RAG_DATABASE_NAME", "PGDATABASE", "POSTGRES_DB")
    )
    secrets = tmp_path / "secrets.env"
    secrets.touch(mode=0o600)
    secrets.write_text("RAG_DATABASE_PASSWORD=" + credentials["password"] + "\n")
    configs = []
    try:
        for _ in range(2):
            name = "rag_adapters_test_" + uuid4().hex[:12]
            with database.connect_database(base, register_pgvector=False) as admin:
                admin.autocommit = True
                admin.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
                )
            config = replace(
                base,
                database=replace(
                    base.database,
                    name=name,
                    user=credentials["user"],
                    credentials_file=secrets,
                ),
            )
            configs.append(config)
            with database.connect_database(config, register_pgvector=False) as c:
                c.execute("CREATE EXTENSION vector")
        yield configs
    finally:
        for config in configs:
            with database.connect_database(base, register_pgvector=False) as admin:
                admin.autocommit = True
                admin.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                        sql.Identifier(config.database.name)
                    )
                )


@pytest.mark.parametrize("binary", [False, True])
@pytest.mark.parametrize("placeholder", ["%t", "%b"])
def test_vector_families_arrays_and_distances(databases, binary, placeholder):
    config = databases[0]
    values = [
        Vector([1, 2, 3]),
        HalfVector([1, 2, 3]),
        SparseVector([1, 2, 3]),
        Bit("101"),
    ]
    with database.connect_database(config) as c:
        assert c.info.transaction_status == TransactionStatus.IDLE
        with c.cursor(binary=binary) as cur:
            for kind, value in zip(
                ("vector", "halfvec", "sparsevec", "bit(3)"), values
            ):
                result = cur.execute(
                    f"SELECT {placeholder}::{kind}::text, {placeholder}::{kind}[], {placeholder}::{kind}",
                    (value, [value, value], value),
                ).fetchone()
                assert len(result[1]) == 2
                if kind != "bit(3)":
                    assert list(result[2].to_list()) == [1, 2, 3]
                    assert list(result[1][1].to_list()) == [1, 2, 3]
                else:
                    assert result[0] == "101"
                if kind != "bit(3)":
                    assert (
                        cur.execute(
                            f"SELECT {placeholder}::{kind} <-> {placeholder}::{kind}",
                            (value, value),
                        ).fetchone()[0]
                        == 0
                    )
        c.execute("CREATE TEMP TABLE adaptation_write (v vector, a vector[])")
        c.execute(
            "INSERT INTO adaptation_write VALUES (%s,%s)", (values[0], [values[0]])
        )
        assert len(c.execute("SELECT a FROM adaptation_write").fetchone()[0]) == 1


def test_concurrent_first_connections_register_once_and_are_not_retained(
    databases, monkeypatch
):
    config = databases[0]
    original, calls, refs = database.register_vector, [], []
    barrier = threading.Barrier(20)

    def register(c):
        calls.append(1)
        return original(c)

    monkeypatch.setattr(database, "register_vector", register)

    def connect(_):
        barrier.wait(timeout=15)
        with database.connect_database(config) as c:
            refs.append(weakref.ref(c))
            assert c.execute("SELECT '[1,2,3]'::vector").fetchone()[0].to_list() == [
                1,
                2,
                3,
            ]

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(connect, range(20)))
    gc.collect()
    assert calls == [1]
    assert all(ref() is None for ref in refs)


def test_database_search_path_and_connection_adapters_are_isolated(databases):
    first, second = databases
    with database.connect_database(first) as a, database.connect_database(second) as b:
        assert a.adapters.types["vector"].oid != b.adapters.types["vector"].oid
        oid = a.adapters.types["vector"].oid

        class LocalLoader(Loader):
            def load(self, data):
                return "local-only"

        a.adapters.register_loader(oid, LocalLoader)
        assert a.execute("SELECT '[1]'::vector").fetchone()[0] == "local-only"
        with database.connect_database(first) as other:
            assert other.execute("SELECT '[1]'::vector").fetchone()[0].to_list() == [1]
        assert b.execute("SELECT '[1]'::vector").fetchone()[0].to_list() == [1]
    different_path = replace(first, index=IndexConfig(schema="adapter_test"))
    with database.connect_database(different_path) as c:
        assert c.execute("SHOW search_path").fetchone()[0] == "adapter_test,public"
    keys = [key for key in database._vector_templates if key[2] == first.database.name]
    assert {key[4] for key in keys} == {"public,public", "adapter_test,public"}
    assert all(len(key) == 5 for key in keys)


def test_extension_oid_change_replaces_template_without_obsolete_loaders(databases):
    config = databases[0]
    with database.connect_database(config) as c:
        old = c.adapters.types["vector"]
    size = len(database._vector_templates)
    with database.connect_database(config, register_pgvector=False) as c:
        c.execute("DROP EXTENSION vector")
        c.execute("CREATE EXTENSION vector")
    with database.connect_database(config) as c:
        new = c.adapters.types["vector"]
        assert new.oid != old.oid and new.array_oid != old.array_oid
        assert c.adapters.get_loader(old.oid, psycopg.pq.Format.TEXT) is None
        assert c.adapters.get_loader(old.array_oid, psycopg.pq.Format.BINARY) is None
        assert c.execute("SELECT '[1]'::vector").fetchone()[0].to_list() == [1]
    assert len(database._vector_templates) == size


def test_initialization_failure_closes_connection_preserves_exception(
    databases, monkeypatch
):
    config = databases[0]
    original, opened = psycopg.connect, []
    failure = RuntimeError("adapter-initialization-test")

    def connect(*args, **kwargs):
        c = original(*args, **kwargs)
        opened.append(c)
        return c

    def fail(c):
        raise failure

    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(database, "register_vector", fail)
    with pytest.raises(RuntimeError) as caught:
        database.connect_database(config)
    assert caught.value is failure
    assert opened and all(c.closed for c in opened)
    assert not any(key[2] == config.database.name for key in database._vector_templates)


def test_no_registration_without_extension_and_transaction_semantics(databases):
    config = databases[0]
    with database.connect_database(config, register_pgvector=False) as c:
        c.execute("DROP EXTENSION vector")
    with pytest.raises(psycopg.ProgrammingError, match="vector type not found"):
        database.connect_database(config)
    with database.connect_database(config, register_pgvector=False) as c:
        assert c.info.transaction_status == TransactionStatus.IDLE
        c.execute("CREATE TABLE transaction_test (value int)")
    with (
        pytest.raises(RuntimeError),
        database.connect_database(config, register_pgvector=False) as c,
    ):
        c.execute("INSERT INTO transaction_test VALUES (1)")
        raise RuntimeError("rollback")
    with database.connect_database(config, register_pgvector=False) as c:
        assert c.execute("SELECT count(*) FROM transaction_test").fetchone()[0] == 0
