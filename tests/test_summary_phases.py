"""Offline checks for the validated generation/publication handoff."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from rag_favorite import knowledge_summaries as summaries
from rag_favorite.config import ConfigError
from rag_favorite.video_config import VideoConfig


class Database:
    def __init__(self):
        self.queries = []
        self.current = None
        self.row = None
        self.autocommit = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        self.queries.append((query, params))
        if "pg_try_advisory_lock" in query:
            self.row = (True,)
        elif "SELECT version,metadata,summary" in query:
            self.row = self.current
        else:
            self.row = None
        return self

    def fetchone(self):
        return self.row

    @property
    def publications(self):
        return [
            params
            for query, params in self.queries
            if "INSERT INTO public.rag_knowledge_summaries" in query
        ]


class Provider:
    def __init__(self, result=None):
        self.calls = 0
        self.result = result or {
            "document_type": "烹饪教程",
            "sections": [
                {
                    "heading": "步骤",
                    "paragraphs": [
                        {
                            "text": "先淘米。",
                            "citations": [{"segment": 0, "quote_id": "0:q0"}],
                        }
                    ],
                }
            ],
        }

    def json(self, *_):
        self.calls += 1
        return self.result


class Encoder:
    def __init__(self):
        self.calls = 0
        self.reject = False

    def embed_documents(self, chunks):
        self.calls += 1
        if self.reject:
            raise RuntimeError("encoder unavailable")
        return [[1.0, 0.0, 0.0] for _ in chunks]


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    job_id = str(uuid4())
    directory = tmp_path / "derived" / job_id
    directory.mkdir(parents=True)
    (directory / "knowledge.md").write_text("先淘米。", encoding="utf-8")
    (directory / "record.json").write_text(
        json.dumps(
            {
                "id": job_id,
                "segments": [
                    {
                        "ordinal": 0,
                        "start": 0,
                        "end": 30,
                        "transcript": "先淘米。",
                        "facts": [],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    config = SimpleNamespace(unified=False)
    profile = SimpleNamespace(
        embedding=SimpleNamespace(dimensions=3),
        index=SimpleNamespace(generation="test-generation"),
    )
    video = VideoConfig(root=tmp_path, credentials_file=tmp_path / "none")
    job = {
        "id": job_id,
        "collection": "general",
        "state": "running",
        "payload": {"title": "测试知识", "source_label": "owner source"},
    }
    database = Database()
    stages, fences = [], []
    monkeypatch.setattr(summaries, "_job", lambda *_: job)
    monkeypatch.setattr(summaries, "connect_database", lambda *_, **__: database)
    monkeypatch.setattr(summaries, "resolve_index", lambda _: profile)
    monkeypatch.setattr(summaries, "embedding_space_id", lambda _: "test-space")
    monkeypatch.setattr(summaries, "assert_write_snapshot", lambda *_: None)
    monkeypatch.setattr(summaries, "assert_owner", lambda c=None: fences.append(c))
    monkeypatch.setattr(
        summaries, "_stage", lambda *args: stages.append(args[3:])
    )
    monkeypatch.setattr(
        summaries,
        "client_from_config",
        lambda _: pytest.fail("unexpected encoder construction"),
    )
    return SimpleNamespace(
        config=config,
        video=video,
        job_id=job_id,
        directory=directory,
        database=database,
        job=job,
        stages=stages,
        fences=fences,
    )


def run(fixture, **kwargs):
    return summaries.summarize_job(
        fixture.config, fixture.video, fixture.job_id, **kwargs
    )


def test_generate_validates_and_persists_without_embedding_or_publication(fixture):
    provider, encoder = Provider(), Encoder()
    generated = run(fixture, provider=provider, encoder=encoder, phase="generate")
    assert generated["state"] == "generated"
    assert provider.calls == 1 and encoder.calls == 0
    assert not fixture.database.publications
    assert fixture.stages[-1] == ("running", "generated")
    checkpoint = json.loads((fixture.directory / "summary-record.json").read_text())
    candidate = (fixture.directory / "summary-candidate.md").read_text()
    assert checkpoint["summary_sha256"] == summaries.text_hash(candidate)
    assert checkpoint["version"] == generated["version"]
    assert not (fixture.directory / "summary.md").exists()
    again = run(fixture, provider=provider, encoder=encoder, phase="generate")
    assert again["reused"] and provider.calls == 1


def test_publish_consumes_checkpoint_without_calling_provider(fixture, monkeypatch):
    provider, encoder = Provider(), Encoder()
    generated = run(fixture, provider=provider, phase="generate")
    monkeypatch.setattr(
        summaries, "CCRProvider", lambda *_, **__: pytest.fail("provider constructed")
    )
    result = run(fixture, encoder=encoder, phase="publish")
    assert result["state"] == "complete" and result["version"] == generated["version"]
    assert encoder.calls == 1 and provider.calls == 1
    assert len(fixture.database.publications) == 1
    assert (fixture.directory / "summary.md").is_file()
    assert fixture.database in fixture.fences


@pytest.mark.parametrize("missing", ["summary-record.json", "summary-candidate.md"])
def test_publish_missing_generation_fails_without_provider_or_encoder(fixture, missing):
    provider, encoder = Provider(), Encoder()
    run(fixture, provider=provider, phase="generate")
    (fixture.directory / missing).unlink()
    with pytest.raises(ConfigError, match="SUMMARY_GENERATION_"):
        run(fixture, provider=provider, encoder=encoder, phase="publish")
    assert provider.calls == 1 and encoder.calls == 0
    assert not fixture.database.publications


@pytest.mark.parametrize("changed", ["draft", "evidence", "timing", "title", "candidate"])
def test_publish_mismatched_generation_fails_closed(fixture, changed):
    provider, encoder = Provider(), Encoder()
    run(fixture, provider=provider, phase="generate")
    if changed == "draft":
        (fixture.directory / "knowledge.md").write_text("先洗米。", encoding="utf-8")
    elif changed == "candidate":
        (fixture.directory / "summary-candidate.md").write_text("tampered")
    elif changed == "title":
        fixture.job["payload"]["title"] = "改变标题"
    else:
        path = fixture.directory / "record.json"
        record = json.loads(path.read_text())
        if changed == "timing":
            record["segments"][0]["start"] = 5
        else:
            record["segments"][0]["transcript"] = "先洗米。"
        path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ConfigError, match="SUMMARY_GENERATION_MISMATCH"):
        run(fixture, provider=provider, encoder=encoder, phase="publish")
    assert provider.calls == 1 and encoder.calls == 0
    assert not fixture.database.publications


def test_invalid_generation_never_creates_a_publishable_checkpoint(fixture):
    raw = Provider().result
    raw["sections"][0]["paragraphs"][0]["text"] = "浸泡30分钟。"
    provider, encoder = Provider(raw), Encoder()
    with pytest.raises(summaries.ProviderUnavailable, match="source validation"):
        run(fixture, provider=provider, encoder=encoder, phase="generate")
    assert provider.calls == 3 and encoder.calls == 0
    assert not (fixture.directory / "summary-record.json").exists()
    assert not fixture.database.publications


def test_failed_publication_preserves_generation_for_retry(fixture):
    provider, encoder = Provider(), Encoder()
    run(fixture, provider=provider, phase="generate")
    checkpoint = (fixture.directory / "summary-record.json").read_text()
    encoder.reject = True
    with pytest.raises(RuntimeError, match="unavailable"):
        run(fixture, provider=provider, encoder=encoder, phase="publish")
    assert (fixture.directory / "summary-record.json").read_text() == checkpoint
    assert not fixture.database.publications
    encoder.reject = False
    assert run(fixture, provider=provider, encoder=encoder, phase="publish")["state"] == "complete"
    assert provider.calls == 1


def test_generation_rejects_source_change_during_provider_call(fixture):
    class ChangingProvider(Provider):
        def json(self, *args):
            (fixture.directory / "knowledge.md").write_text("改变原文。", encoding="utf-8")
            return super().json(*args)

    with pytest.raises(ConfigError, match="SUMMARY_DRAFT_CHANGED"):
        run(fixture, provider=ChangingProvider(), phase="generate")
    assert not (fixture.directory / "summary-record.json").exists()
    assert not fixture.database.publications


def test_publication_rejects_evidence_change_during_embedding(fixture):
    run(fixture, provider=Provider(), phase="generate")

    class ChangingEncoder(Encoder):
        def embed_documents(self, chunks):
            path = fixture.directory / "record.json"
            record = json.loads(path.read_text())
            record["segments"][0]["end"] = 35
            path.write_text(json.dumps(record), encoding="utf-8")
            return super().embed_documents(chunks)

    with pytest.raises(ConfigError, match="SUMMARY_EVIDENCE_CHANGED"):
        run(fixture, encoder=ChangingEncoder(), phase="publish")
    assert not fixture.database.publications


def test_default_phase_still_generates_and_publishes(fixture):
    provider, encoder = Provider(), Encoder()
    result = run(fixture, provider=provider, encoder=encoder)
    assert result["state"] == "complete"
    assert provider.calls == encoder.calls == 1
    assert len(fixture.database.publications) == 1


def test_cached_published_summary_does_not_repeat_generation_or_embedding(fixture):
    provider, encoder = Provider(), Encoder()
    result = run(fixture, provider=provider, encoder=encoder)
    publication = fixture.database.publications[0]
    metadata = publication[-1].obj
    fixture.database.current = (result["version"], metadata, publication[8])
    assert run(fixture, provider=provider, encoder=encoder, phase="generate")["reused"]
    assert run(fixture, provider=provider, encoder=encoder, phase="publish")["reused"]
    assert provider.calls == encoder.calls == 1


def test_unknown_phase_is_rejected_before_loading_job(fixture):
    with pytest.raises(ValueError, match="phase"):
        run(fixture, phase="unknown")
    assert not fixture.database.queries
