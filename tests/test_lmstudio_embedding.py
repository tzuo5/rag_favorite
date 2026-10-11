import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from rag_favorite.config import EmbeddingConfig, default_config, load_config
from rag_favorite.embedding import (
    EmbeddingError,
    LMStudioEmbeddingClient,
    client_from_config,
    embedding_space_id,
)


@pytest.fixture
def local_model(tmp_path, monkeypatch):
    home = tmp_path / ".lmstudio"
    root = home / "models"
    file = root / "vendor/repo/fixture.gguf"
    file.parent.mkdir(parents=True)
    file.write_bytes(b"fixture model weights")
    home.joinpath("settings.json").write_text(
        json.dumps({"downloadsFolder": str(root)})
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    metadata = {
        "identifier": "fixture",
        "modelKey": "fixture-key",
        "type": "embedding",
        "format": "gguf",
        "path": "vendor/repo/fixture.gguf",
        "deviceIdentifier": None,
        "contextLength": 2048,
    }

    def cli(argv, **_kwargs):
        assert argv[1:] == ["ps", "--json"]
        return SimpleNamespace(stdout=json.dumps([metadata]))

    monkeypatch.setattr("rag_favorite.embedding.subprocess.run", cli)
    settings = replace(
        EmbeddingConfig(),
        model="fixture",
        dimensions=2,
        model_digest=hashlib.sha256(file.read_bytes()).hexdigest(),
    )
    return settings, file, metadata


class Opener:
    def __init__(self):
        self.requests = []
        self.rows = None
        self.changed = None
        self.inventory = {
            "models": [
                {
                    "type": "embedding",
                    "key": "fixture-key",
                    "format": "gguf",
                    "loaded_instances": [
                        {"id": "fixture", "config": {"context_length": 2048}}
                    ],
                }
            ]
        }

    @contextmanager
    def urlopen(self, request, **_kwargs):
        self.requests.append(request)
        if request.data:
            inputs = json.loads(request.data)["input"]
            rows = (
                self.rows
                if self.rows is not None
                else [
                    {"index": i, "embedding": [3, 4]}
                    for i in reversed(range(len(inputs)))
                ]
            )
            result = {"model": "fixture", "data": rows}
            if self.changed:
                self.changed()
        else:
            result = self.inventory
        yield SimpleNamespace(read=lambda _limit: json.dumps(result).encode())


def test_real_provider_transport_batches_queries_and_orders_vectors(
    local_model, monkeypatch
):
    settings, _, _ = local_model
    opener = Opener()
    monkeypatch.setenv("LM_API_TOKEN", "fixture-auth-token")
    client = LMStudioEmbeddingClient(
        replace(settings, batch_size=2, normalization="l2"), opener=opener
    )
    assert client.embed_documents(["甲", "乙", "丙"]) == [(0.6, 0.8)] * 3
    assert client.embed_query("料汁几勺？") == (0.6, 0.8)
    posts = [r for r in opener.requests if r.data]
    assert [json.loads(r.data)["input"] for r in posts] == [
        ["甲", "乙"],
        ["丙"],
        [settings.query_instruction + "料汁几勺？"],
    ]
    assert all(r.full_url.endswith("/v1/embeddings") for r in posts)
    assert all(
        r.get_header("Authorization") == "Bearer fixture-auth-token"
        for r in opener.requests
    )
    assert all("options" not in json.loads(r.data) for r in posts)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"index": 1, "embedding": [1, 2]}],
        [{"index": True, "embedding": [1, 2]}],
        [{"index": 0, "embedding": [1]}],
        [{"index": 0, "embedding": [True, 1]}],
        [{"index": 0, "embedding": [float("nan"), 1]}],
        [{"index": 0, "embedding": [float("inf"), 1]}],
        [{"index": 0, "embedding": [0, 0]}],
    ],
)
def test_invalid_vectors_rejected(local_model, rows):
    opener = Opener()
    opener.rows = rows
    with pytest.raises(EmbeddingError):
        LMStudioEmbeddingClient(local_model[0], opener=opener).embed_document("doc")


def test_digest_change_during_request_rejects_result(local_model):
    settings, file, _ = local_model
    opener = Opener()
    opener.changed = lambda: file.write_bytes(b"replacement model")
    with pytest.raises(EmbeddingError, match="digest differs"):
        LMStudioEmbeddingClient(settings, opener=opener).embed_query("query")


def test_wrong_file_binding_and_remote_device_rejected(local_model):
    settings, _, metadata = local_model
    metadata["modelKey"] = "wrong-file"
    with pytest.raises(EmbeddingError, match="identity differ"):
        LMStudioEmbeddingClient(settings, opener=Opener()).embed_query("query")
    metadata["modelKey"] = "fixture-key"
    metadata["deviceIdentifier"] = "remote-device"
    with pytest.raises(EmbeddingError, match="identity differ"):
        LMStudioEmbeddingClient(settings, opener=Opener()).embed_query("query")


def test_unpinned_long_input_and_unloaded_model_never_encode(local_model):
    settings, _, _ = local_model
    opener = Opener()
    with pytest.raises(EmbeddingError, match="not pinned"):
        LMStudioEmbeddingClient(
            replace(settings, model_digest=""), opener=opener
        ).embed_query("query")
    assert not opener.requests
    with pytest.raises(EmbeddingError, match="context limit") as error:
        LMStudioEmbeddingClient(settings, opener=opener).embed_document("中" * 1000)
    assert error.value.code == "EMBEDDING_CONTEXT_LIMIT"
    assert not any(r.data for r in opener.requests)
    opener.inventory = {"models": []}
    with pytest.raises(EmbeddingError, match="not loaded"):
        LMStudioEmbeddingClient(settings, opener=opener).embed_query("query")


def test_document_budget_matches_actual_loaded_context(local_model):
    settings, _, metadata = local_model
    opener = Opener()
    client = LMStudioEmbeddingClient(settings, opener=opener)
    assert client.document_byte_limit() == 2032
    metadata["contextLength"] = 4096
    opener.inventory["models"][0]["loaded_instances"][0]["config"]["context_length"] = (
        4096
    )
    assert client.document_byte_limit() == 4080


def test_dense_text_helper_uses_verified_budget_with_real_client_transport(local_model):
    from rag_favorite.video_visual import embed_complete_text

    opener = Opener()
    client = LMStudioEmbeddingClient(local_model[0], opener=opener)
    value = "中文知识😀" * 700
    result = embed_complete_text(client, value)
    inputs = [
        text
        for request in opener.requests
        if request.data
        for text in json.loads(request.data)["input"]
    ]
    assert "".join(inputs) == value
    assert all(len(text.encode("utf-8")) <= 2032 for text in inputs)
    assert len(inputs) > 1 and result == [0.6, 0.8]


def test_lmstudio_defaults_and_legacy_explicit_config(tmp_path):
    assert isinstance(client_from_config(default_config()), LMStudioEmbeddingClient)
    profile = tmp_path / "config.toml"
    profile.write_text('[embedding]\nbackend="ollama"\n')
    legacy = load_config(profile).embedding
    assert legacy.url == "http://127.0.0.1:11434/api/embed"
    assert legacy.model == "qwen3-embedding:0.6b"
    assert embedding_space_id(legacy) != embedding_space_id(EmbeddingConfig())
    studio = EmbeddingConfig()
    assert embedding_space_id(studio) != embedding_space_id(
        replace(studio, query_instruction="changed")
    )
    assert embedding_space_id(studio) != embedding_space_id(
        replace(studio, model_digest="a" * 64)
    )


def test_local_provider_refuses_non_loopback_and_credentials_in_url():
    for url in (
        "https://provider.example/v1/embeddings",
        "http://token@127.0.0.1:1234/v1/embeddings",
    ):
        with pytest.raises(EmbeddingError, match="loopback"):
            LMStudioEmbeddingClient(replace(EmbeddingConfig(), url=url))
