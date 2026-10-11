import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from rag_favorite.config import EmbeddingConfig
from rag_favorite.embedding import (
    EmbeddingError,
    OllamaEmbeddingClient,
    embedding_space_id,
)

DIGEST = "a" * 64


class LocalOpener:
    def __init__(self, vectors=None):
        self.digest = DIGEST
        self.requests = []
        self.vectors = vectors
        self.changed_after_embed = False

    @contextmanager
    def urlopen(self, request, **kwargs):
        self.requests.append(request)
        if request.full_url.endswith("/tags"):
            result = {"models": [{"name": "fixture:1", "digest": self.digest}]}
        else:
            count = len(json.loads(request.data)["input"])
            result = {
                "model": "fixture:1",
                "embeddings": self.vectors
                if self.vectors is not None
                else [[0.6, 0.8]] * count,
            }
            if self.changed_after_embed:
                self.digest = "b" * 64
        yield SimpleNamespace(read=lambda _: json.dumps(result).encode())


def settings(**kwargs):
    return replace(
        EmbeddingConfig(
            backend="ollama",
            url="http://127.0.0.1:11434/api/embed",
            model="fixture:1",
            model_digest=DIGEST,
            dimensions=2,
        ),
        **kwargs,
    )


def test_local_batching_document_query_and_no_truncation():
    opener = LocalOpener()
    client = OllamaEmbeddingClient(settings(batch_size=2), opener=opener)
    assert client.embed_documents(["甲", "乙", "丙"]) == [(0.6, 0.8)] * 3
    client.embed_query("料汁配方")
    payloads = [json.loads(r.data) for r in opener.requests if r.data]
    assert [p["input"] for p in payloads[:2]] == [["甲", "乙"], ["丙"]]
    assert payloads[-1]["input"] == [client.settings.query_instruction + "料汁配方"]
    assert all(p["truncate"] is False and p["dimensions"] == 2 for p in payloads)
    assert all(r.full_url.startswith("http://127.0.0.1:") for r in opener.requests)


@pytest.mark.parametrize(
    "vectors",
    [
        [],
        [[1]],
        [[True, 1]],
        [[float("nan"), 1]],
        [[float("inf"), 1]],
        [[0, 0]],
        [["1", 1]],
    ],
)
def test_local_rejects_invalid_vectors(vectors):
    with pytest.raises(EmbeddingError):
        OllamaEmbeddingClient(settings(), opener=LocalOpener(vectors)).embed_query(
            "query"
        )


def test_unpinned_or_changed_digest_blocks_embedding_request():
    opener = LocalOpener()
    with pytest.raises(EmbeddingError, match="not pinned"):
        OllamaEmbeddingClient(settings(model_digest=""), opener=opener).embed_query(
            "query"
        )
    assert opener.requests == []
    opener.digest = "b" * 64
    with pytest.raises(EmbeddingError, match="digest differs"):
        OllamaEmbeddingClient(settings(), opener=opener).embed_query("query")
    assert not any(r.data for r in opener.requests)


def test_mid_request_model_change_rejects_returned_vector():
    opener = LocalOpener()
    opener.changed_after_embed = True
    with pytest.raises(EmbeddingError, match="digest differs"):
        OllamaEmbeddingClient(settings(), opener=opener).embed_document("document")


@pytest.mark.parametrize(
    "change",
    [
        {"model_digest": "b" * 64},
        {"query_instruction": "other"},
        {"preprocessing_version": "v2"},
        {"revision": "2"},
        {"normalization": "l2"},
    ],
)
def test_same_dimension_different_encoder_contract_changes_space(change):
    assert embedding_space_id(settings()) != embedding_space_id(settings(**change))
