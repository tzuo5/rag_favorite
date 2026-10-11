from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from rag_favorite.config import (
    ConfigError,
    EmbeddingConfig,
    default_config,
    load_config,
)
from rag_favorite.embedding import (
    EmbeddingChargeUnknown,
    EmbeddingError,
    OpenRouterEmbeddingClient,
    client_from_config,
    embedding_space_id,
    index_model_id,
)
from rag_favorite.rag import ensure_embedding_space


class Opener:
    def __init__(self, result):
        self.result, self.requests = result, []

    @contextmanager
    def urlopen(self, request, **kwargs):
        self.requests.append(request)
        yield SimpleNamespace(read=lambda _: json.dumps(self.result).encode())


def settings():
    return EmbeddingConfig(
        backend="openrouter",
        api_key_env="OPENROUTER_API_KEY",
        url="https://openrouter.ai/api/v1/embeddings",
        model="perplexity/pplx-embed-v1-0.6b",
        dimensions=2,
    )


def test_embedding_response_is_reordered_and_queries_have_no_qwen_prefix(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-secret")
    opener = Opener(
        {
            "data": [
                {"index": 1, "embedding": [3, 4]},
                {"index": 0, "embedding": [1, 2]},
            ],
            "usage": {"cost": 0.0001},
        }
    )
    client = OpenRouterEmbeddingClient(settings(), opener=opener)
    assert client.embed_documents(["a", "b"]) == [(1.0, 2.0), (3.0, 4.0)]
    opener.result = {"data": [{"index": 0, "embedding": [1, 2]}]}
    client.embed_query("生抽多少")
    assert json.loads(opener.requests[-1].data)["input"] == ["生抽多少"]
    assert opener.requests[-1].get_header("Authorization") == "Bearer fixture-secret"


@pytest.mark.parametrize(
    "data",
    [
        [],
        [{"index": 0, "embedding": [1]}],
        [{"index": 1, "embedding": [1, 2]}],
        [{"index": True, "embedding": [1, 2]}],
        [{"index": 0, "embedding": [True, 2]}],
        [{"index": 0, "embedding": [float("nan"), 2]}],
        [{"index": 0, "embedding": [float("inf"), 2]}],
        [{"index": 0, "embedding": ["1", 2]}],
    ],
)
def test_invalid_embeddings_are_rejected(monkeypatch, data):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture")
    client = OpenRouterEmbeddingClient(settings(), opener=Opener({"data": data}))
    with pytest.raises(EmbeddingError):
        client.embed_query("query")


def test_missing_embedding_key_never_makes_network_request(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    opener = Opener({})
    with pytest.raises(EmbeddingError, match="credential"):
        OpenRouterEmbeddingClient(settings(), opener=opener).embed_query("query")
    assert opener.requests == []


def test_openrouter_config_has_independent_space_and_factory(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[embedding]\nbackend="openrouter"\n')
    config = load_config(path)
    assert isinstance(client_from_config(config), OpenRouterEmbeddingClient)
    assert config.embedding.model == "perplexity/pplx-embed-v1-0.6b"
    assert config.embedding.dimensions == default_config().embedding.dimensions
    assert embedding_space_id(config.embedding) != embedding_space_id(
        default_config().embedding
    )
    assert index_model_id(config.embedding) != index_model_id(
        default_config().embedding
    )
    assert embedding_space_id(config.embedding) != embedding_space_id(
        replace(config.embedding, revision="2")
    )


def test_mixed_database_spaces_block_ingestion_and_queries_before_api(monkeypatch):
    config = replace(default_config(), embedding=settings())

    @contextmanager
    def connect(_, **kwargs):
        yield SimpleNamespace(
            execute=lambda *args: SimpleNamespace(
                fetchone=lambda: ("qwen3-embedding:0.6b",)
            )
        )

    monkeypatch.setattr("rag_favorite.rag.connect_database", connect)
    monkeypatch.setattr("rag_favorite.indexes.connect_database", connect)
    with pytest.raises(ConfigError, match="space mismatch"):
        ensure_embedding_space(config)


def test_empty_test_database_accepts_new_space(monkeypatch):
    @contextmanager
    def connect(_, **kwargs):
        yield SimpleNamespace(
            execute=lambda *args: SimpleNamespace(fetchone=lambda: None)
        )

    monkeypatch.setattr("rag_favorite.rag.connect_database", connect)
    monkeypatch.setattr("rag_favorite.indexes.connect_database", connect)
    ensure_embedding_space(replace(default_config(), embedding=settings()))


def test_embedding_usage_is_durable_and_missing_cost_stays_unknown(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-secret")
    audit = tmp_path / "usage.jsonl"
    client = OpenRouterEmbeddingClient(
        settings(),
        usage_path=audit,
        opener=Opener({"data": [{"index": 0, "embedding": [1, 2]}]}),
    )
    client.embed_query("private query")
    rows = [json.loads(line) for line in audit.read_text().splitlines()]
    assert rows[0]["state"] == "reserved"
    assert rows[1]["state"] == "usage_unknown" and rows[1]["actual_cost_usd"] is None
    assert (
        "fixture-secret" not in audit.read_text()
        and "private query" not in audit.read_text()
    )


def test_lost_embedding_response_is_marked_non_replayable(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture")

    class BrokenOpener:
        def urlopen(self, *args, **kwargs):
            raise TimeoutError("response lost")

    audit = tmp_path / "usage.jsonl"
    client = OpenRouterEmbeddingClient(
        settings(), usage_path=audit, opener=BrokenOpener()
    )
    with pytest.raises(EmbeddingChargeUnknown) as result:
        client.embed_query("query")
    assert result.value.charge_unknown
    assert json.loads(audit.read_text().splitlines()[-1])["state"] == "usage_unknown"
