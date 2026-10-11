"""Source fidelity, summary-only MCP routing and opt-in isolated PostgreSQL checks."""

import asyncio
import json
import os
from dataclasses import replace
from uuid import uuid4

import pytest
from pgvector import Vector
from psycopg.types.json import Jsonb

from rag_favorite import knowledge_summaries as summaries
from rag_favorite import video_retrieval
from rag_favorite.config import (
    CollectionConfig,
    ConfigError,
    default_config,
    load_config,
)
from rag_favorite.database import connect_database
from rag_favorite.indexes import resolve_index
from rag_favorite.paths import AppPaths
from rag_favorite.queue_control import set_control
from rag_favorite.summary_worker import (
    claim_summary,
    enqueue_summaries,
    recover_summary_claims,
)
from rag_favorite.video_config import VideoConfig
from rag_favorite.video_mcp import create_video_mcp
from rag_favorite.video_provider import ProviderUnavailable
from rag_favorite.video_retention import retry_job
from rag_favorite.video_store import library_id


def response(text="先淘米，再浸泡。", quote="先淘米，再浸泡。"):
    return {
        "document_type": "烹饪教程",
        "sections": [
            {
                "heading": "步骤",
                "paragraphs": [
                    {"text": text, "citations": [{"segment": 0, "quote": quote}]}
                ],
            }
        ],
    }


def segment(text="先淘米，再浸泡。", facts=None):
    return {
        "ordinal": 0,
        "start": 0.0,
        "end": 30.0,
        "transcript": text,
        "facts": facts or [],
        "caption": "",
    }


def render(raw, segments):
    return summaries.render_summary(
        raw, title="寿司米", source="owner video", segments=segments
    )


def test_summary_retains_disputed_numeric_evidence_and_real_time():
    s = segment(
        "盐10克", [{"quote": "盐10克", "statement": "盐10克", "conflict": True}]
    )
    result = render(response("盐10克。", "盐10克"), [s])
    assert "存在不确定或冲突" in result
    assert "**冲突**：盐10克" in result and "0–30 秒" in result


def test_generic_counts_of_conflicting_descriptions_are_not_recipe_quantities():
    raw = response("两种范围不一致，不把其中一种范围当作确定用量。", "不一致")
    assert "两种范围不一致" in render(raw, [segment("不一致")])
    raw = response("两种说法不一致，无法确定统一比例。", "不一致")
    assert "统一比例" in render(raw, [segment("不一致")])


def test_summary_citations_can_select_deterministic_quote_ids():
    raw = response("盐10克。")
    raw["sections"][0]["paragraphs"][0]["citations"] = [
        {"segment": 0, "quote_id": "0:q0"}
    ]
    assert "盐10克" in render(raw, [segment("盐10克")])
    raw["sections"][0]["paragraphs"][0]["citations"][0]["quote_id"] = "0:invented"
    with pytest.raises(ValueError, match="quote ID"):
        render(raw, [segment("盐10克")])


def test_long_summary_paragraph_can_cite_more_than_eight_source_quotes():
    raw = response("需要综合这些记录。")
    raw["sections"][0]["paragraphs"][0]["citations"] = [
        {"segment": 0, "quote_id": f"0:q{i}"} for i in range(9)
    ]
    assert "综合这些记录" in render(
        raw, [segment("\n".join(f"原始记录{i}" for i in range(9)))]
    )


@pytest.mark.parametrize(
    "raw",
    [
        response("盐30克。", "盐10克"),
        response("盐三十克。", "盐10克"),
        response("先淘米。", "外部不存在的证据"),
    ],
)
def test_summary_rejects_guessed_numbers_and_fabricated_quotes(raw):
    with pytest.raises(ValueError, match="Unsupported"):
        render(raw, [segment("盐10克")])


def test_summary_rejects_missing_citations_and_unchecked_numeric_headings():
    raw = response()
    raw["sections"][0]["paragraphs"][0]["citations"] = []
    with pytest.raises(ValueError, match="citations"):
        render(raw, [segment()])
    raw = response()
    raw["sections"][0]["heading"] = "浸泡15分钟"
    with pytest.raises(ValueError, match="numeric"):
        render(raw, [segment()])


def test_summary_chunking_respects_utf8_limit_without_losing_text():
    class SmallEncoder:
        def document_byte_limit(self):
            return 11

    text = "中文寿司 🍚 文档连续内容"
    chunks = summaries.summary_chunks(text, SmallEncoder())
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-8")) <= 11 for chunk in chunks)


def test_search_routes_only_to_summaries_and_rejects_visual_query(monkeypatch):
    calls = []
    monkeypatch.setattr(
        video_retrieval, "search_summaries", lambda *args: calls.append(args) or []
    )
    monkeypatch.setattr(
        video_retrieval,
        "search_video_rankings",
        lambda *a, **k: pytest.fail("Raw rankings called"),
    )
    assert video_retrieval.search_all("寿司", ["cooking"], 5, None, None) == []
    assert len(calls) == 1
    with pytest.raises(ValueError, match="visual_query"):
        video_retrieval.search_all(
            "寿司", ["cooking"], 5, None, None, visual_query="rice"
        )


def test_mcp_summary_contract_top5_and_document_read(tmp_path, monkeypatch):
    from rag_favorite import video_mcp

    config = default_config(AppPaths.discover())
    video = VideoConfig(root=tmp_path, credentials_file=tmp_path / "none")
    calls = []
    monkeypatch.setattr(video_mcp, "search_all", lambda *a, **k: calls.append(a) or [])
    monkeypatch.setattr(
        video_mcp,
        "document_context",
        lambda *a, **k: {
            "ok": True,
            "summary": "总结",
            "original_document": "原文",
            "complete": True,
        },
    )
    server = create_video_mcp(config, video)
    definitions = {t.name: t for t in asyncio.run(server.list_tools())}
    assert definitions["rag_search"].inputSchema["properties"]["limit"]["default"] == 5
    assert definitions["document_read"].annotations.readOnlyHint
    asyncio.run(server.call_tool("rag_search", {"query": "寿司"}))
    assert calls[0][2] == 5
    result = server._tool_manager.get_tool("document_read").fn("video:" + str(uuid4()))
    assert result["summary"] == "总结" and result["original_document"] == "原文"
    with pytest.raises(ValueError):
        server._tool_manager.get_tool("rag_search").fn("寿司", limit=6)


@pytest.fixture
def isolated(tmp_path):
    path = os.environ.get("RAG_SUMMARY_TEST_CONFIG")
    if not path:
        pytest.skip("Set RAG_SUMMARY_TEST_CONFIG for isolated live PostgreSQL checks")
    # These checks exercise the preserved native/legacy contract, independent
    # of the owner's currently deployed feature mode and knowledge directory.
    config = replace(
        resolve_index(load_config(path)),
        unified=False,
        collections={
            "general": CollectionConfig("general", "Test", tmp_path / "knowledge")
        },
    )
    video = VideoConfig(root=tmp_path / "owned", credentials_file=tmp_path / "none")
    ids = [str(uuid4()) for _ in range(10)]
    collection = next(iter(config.collections))
    with connect_database(config) as db:
        for i, job_id in enumerate(ids):
            db.execute(
                "INSERT INTO public.rag_video_jobs(id,library_id,collection,state,payload) VALUES(%s,%s,%s,%s,%s)",
                (
                    job_id,
                    library_id(video) if i != 8 else "foreign-" + job_id,
                    collection,
                    "running" if i == 9 else "complete",
                    Jsonb({"title": "测试知识", "source_label": "owner source"}),
                ),
            )
            directory = video.root / "derived" / job_id
            directory.mkdir(parents=True)
            (directory / "knowledge.md").write_text(
                "先淘米，再浸泡。\n原文独有的秘密参数。", encoding="utf-8"
            )
    try:
        yield config, video, ids, collection
    finally:
        with connect_database(config) as db:
            db.execute("DELETE FROM public.rag_videos WHERE id=ANY(%s::uuid[])", (ids,))
            db.execute(
                "DELETE FROM public.rag_video_jobs WHERE id=ANY(%s::uuid[])", (ids,)
            )


def test_live_summary_retry_uses_saved_draft_even_after_media_expiry(isolated):
    config, video, ids, _ = isolated
    with connect_database(config) as db:
        db.execute(
            "UPDATE public.rag_video_jobs SET state='blocked',stage='media_expired',payload=payload || '{\"media_expired\":true}' WHERE id=%s",
            (ids[0],),
        )
    with pytest.raises(ConfigError, match="expired"):
        retry_job(config, video, ids[0])
    (video.root / "derived" / ids[0] / "record.json").write_text(
        '{"id":"' + ids[0] + '","segments":[]}'
    )
    retry_job(config, video, ids[0])
    with connect_database(config) as db:
        assert (
            db.execute(
                "SELECT state FROM public.rag_video_jobs WHERE id=%s", (ids[0],)
            ).fetchone()[0]
            == "queued"
        )


class Provider:
    def __init__(self):
        self.calls = 0

    def json(self, *args):
        self.calls += 1
        return response()


class Encoder:
    def __init__(self, dimensions):
        self.dimensions = dimensions
        self.texts = []

    def embed_documents(self, texts):
        self.texts.extend(texts)
        return [tuple([1.0] + [0.0] * (self.dimensions - 1)) for _ in texts]

    def embed_query(self, query):
        return tuple([1.0] + [0.0] * (self.dimensions - 1))


def test_live_summary_claim_respects_pause_and_library_scope(isolated):
    config, video, ids, _ = isolated
    assert enqueue_summaries(config, video) == 8
    set_control(video, paused=True)
    assert claim_summary(config, video) is None
    set_control(video, paused=False)
    set_control(video, job_id=ids[0], job_paused=True)
    claimed = claim_summary(config, video)
    assert claimed in ids[1:8]
    with connect_database(config) as db:
        assert (
            db.execute(
                "SELECT state FROM public.rag_summary_jobs WHERE job_id=%s", (claimed,)
            ).fetchone()[0]
            == "running"
        )


def test_live_recovery_preserves_active_summarization_and_recovers_an_abandoned_claim(
    isolated,
):
    config, video, ids, _ = isolated
    summaries._stage(config, video, ids[0], "running", "summarize")
    with connect_database(config, register_pgvector=False) as guard:
        guard.autocommit = True
        guard.execute("SELECT pg_advisory_lock(hashtext(%s))", ("summary:" + ids[0],))
        assert recover_summary_claims(config, video) == 0
    assert recover_summary_claims(config, video) == 1
    with connect_database(config) as db:
        assert (
            db.execute(
                "SELECT state FROM public.rag_summary_jobs WHERE job_id=%s", (ids[0],)
            ).fetchone()[0]
            == "queued"
        )


def test_live_generated_response_is_validated_and_repaired_before_publication(isolated):
    config, video, ids, _ = isolated
    encoder = Encoder(resolve_index(config).embedding.dimensions)

    class RepairProvider(Provider):
        def json(self, instruction, request):
            self.calls += 1
            if self.calls == 1:
                return response("盐30克。", "先淘米，再浸泡。")
            assert "上次结果未通过校验" in instruction
            return response()

    provider = RepairProvider()
    result = summaries.summarize_job(
        config, video, ids[0], provider=provider, encoder=encoder
    )
    assert result["state"] == "complete" and provider.calls == 2


@pytest.mark.parametrize(
    "code,expected_calls",
    [("CCR_RESPONSE_INVALID_JSON", 2), ("CCR_RESPONSE_TRUNCATED", 1)],
)
def test_live_only_completed_malformed_json_is_automatically_retried(
    isolated, code, expected_calls
):
    config, video, ids, _ = isolated
    encoder = Encoder(resolve_index(config).embedding.dimensions)

    class BrokenProvider(Provider):
        def json(self, instruction, request):
            self.calls += 1
            if self.calls == 1:
                raise ProviderUnavailable("invalid response", code=code)
            return response()

    provider = BrokenProvider()
    if code == "CCR_RESPONSE_TRUNCATED":
        with pytest.raises(ProviderUnavailable):
            summaries.summarize_job(
                config, video, ids[0], provider=provider, encoder=encoder
            )
    else:
        result = summaries.summarize_job(
            config, video, ids[0], provider=provider, encoder=encoder
        )
        assert result["state"] == "complete"
    assert provider.calls == expected_calls


def test_live_publication_idempotence_pagination_and_version_guard(isolated):
    config, video, ids, collection = isolated
    encoder = Encoder(resolve_index(config).embedding.dimensions)
    provider = Provider()
    result = summaries.summarize_job(
        config, video, ids[0], provider=provider, encoder=encoder
    )
    again = summaries.summarize_job(
        config, video, ids[0], provider=provider, encoder=encoder
    )
    assert again["reused"] and provider.calls == 1
    assert all("原文独有的秘密参数" not in t for t in encoder.texts)
    version = result["version"]
    full = summaries.document_context(
        "video:" + ids[0], [collection], config, video, version=version
    )
    pages, offset = [], 0
    while True:
        page = summaries.document_context(
            "video:" + ids[0],
            [collection],
            config,
            video,
            version=version,
            offset=offset,
            max_characters=3,
        )
        assert page["summary"] == full["summary"]
        pages.append(page["original_document"])
        if page["complete"]:
            break
        offset = page["next_offset"]
    assert "".join(pages) == full["original_document"]
    with connect_database(config) as db:
        db.execute(
            """INSERT INTO public.rag_videos(id,library_id,collection,title,source_label,
            content_sha256,classification,transcript,provider_usage,source_deleted)
            VALUES(%s,%s,%s,'fixture','owner','sha','{}','[]','[]',false)""",
            (ids[0], library_id(video), collection),
        )
    with pytest.raises(ConfigError):
        summaries.document_context("video:" + ids[0], [collection], config, video)
    with connect_database(config) as db:
        db.execute(
            "UPDATE public.rag_videos SET source_deleted=true WHERE id=%s", (ids[0],)
        )
    with pytest.raises(ConfigError, match="VERSION_CHANGED"):
        summaries.document_context(
            "video:" + ids[0], [collection], config, video, version="stale"
        )
    with pytest.raises(ValueError, match="version"):
        summaries.document_context(
            "video:" + ids[0], [collection], config, video, offset=1
        )
    with pytest.raises(ConfigError):
        summaries.document_context(
            "video:" + ids[0],
            [collection],
            config,
            replace(video, root=video.root / "other"),
        )


def test_live_top5_deduplicates_all_chunks_and_excludes_unpublished_foreign_sources(
    isolated, monkeypatch
):
    config, video, ids, collection = isolated
    profile = resolve_index(config)
    encoder = Encoder(profile.embedding.dimensions)
    monkeypatch.setattr(summaries, "client_from_config", lambda c: encoder)
    for i in [*range(7), 9]:
        summaries.summarize_job(
            config, video, ids[i], provider=Provider(), encoder=encoder
        )
    with connect_database(config) as db:
        # A fully published foreign-library row also remains invisible.
        db.execute(
            """INSERT INTO public.rag_knowledge_summaries(job_id,library_id,collection,title,source_label,
            version,draft_sha256,summary_sha256,summary,draft,embedding_space_id,metadata)
            SELECT %s,%s,collection,title,source_label,version,draft_sha256,summary_sha256,summary,draft,embedding_space_id,metadata
            FROM public.rag_knowledge_summaries WHERE job_id=%s""",
            (ids[8], "foreign-" + ids[8], ids[0]),
        )
        db.execute(
            "INSERT INTO public.rag_summary_chunks(job_id,ordinal,content,embedding) VALUES(%s,0,'外部总结',%s)",
            (ids[8], Vector(list(encoder.embed_query("fixture")))),
        )
        # Duplicate high-scoring chunks must not crowd out other documents.
        db.execute(
            "INSERT INTO public.rag_summary_chunks(job_id,ordinal,content,embedding) VALUES(%s,1,'总结片段',%s)",
            (ids[0], Vector(list(encoder.embed_query("fixture")))),
        )
        # Different cosine values prove sorting by actual similarity.
        for i in range(1, 7):
            vector = [1.0, float(i)] + [0.0] * (encoder.dimensions - 2)
            db.execute(
                "UPDATE public.rag_summary_chunks SET embedding=%s WHERE job_id=%s",
                (Vector(vector), ids[i]),
            )
    result = summaries.search_summaries("问题", [collection], 5, config, video)
    assert [r["document_id"] for r in result] == ["video:" + i for i in ids[:5]]
    assert len({r["document_id"] for r in result}) == 5
    assert result[0]["semantic_score"] == pytest.approx(1.0)
    assert result[1]["semantic_score"] == pytest.approx(2**-0.5)
    assert all("原文独有的秘密参数" not in r["excerpt"] for r in result)
    with pytest.raises(ValueError):
        summaries.search_summaries("问题", [collection], 6, config, video)


def test_live_index_failure_preserves_previous_publication_and_reuses_generation(
    isolated,
):
    config, video, ids, collection = isolated
    encoder = Encoder(resolve_index(config).embedding.dimensions)
    provider = Provider()
    initial = summaries.summarize_job(
        config, video, ids[0], provider=provider, encoder=encoder
    )
    final_path = video.root / "derived" / ids[0] / "summary.md"
    previous_final = final_path.read_text()
    path = video.root / "derived" / ids[0] / "knowledge.md"
    path.write_text(path.read_text() + "\n新增原文。", encoding="utf-8")

    class FailedEncoder:
        def embed_documents(self, texts):
            raise RuntimeError("embedding offline")

    with pytest.raises(RuntimeError):
        summaries.summarize_job(
            config, video, ids[0], provider=provider, encoder=FailedEncoder()
        )
    assert final_path.read_text() == previous_final
    context = summaries.document_context(
        "video:" + ids[0], [collection], config, video, version=initial["version"]
    )
    assert "新增原文" not in context["original_document"]
    assert summaries.summary_status(config, video)["job_states"]["failed"] == 1
    assert enqueue_summaries(config, video, retry_failed=True) > 0
    summaries.summarize_job(config, video, ids[0], provider=provider, encoder=encoder)
    assert provider.calls == 2  # Index retry does not call the LLM again.
    assert summaries.summary_status(config, video)["job_states"].get("failed", 0) == 0


def test_live_retry_expired_source_redownloads_without_discarding_evidence(isolated):
    config, video, ids, _ = isolated
    source = "https://www.xiaohongshu.com/explore/" + "a" * 24
    with connect_database(config) as db:
        db.execute(
            "UPDATE public.rag_video_jobs SET state='blocked',stage='media_expired',payload=payload || %s::jsonb WHERE id=%s",
            (
                json.dumps(
                    {
                        "media_expired": True,
                        "source_url": source,
                        "asset_id": "b" * 32,
                        "sha256": "old",
                    }
                ),
                ids[0],
            ),
        )
    retry_job(config, video, ids[0])
    with connect_database(config) as db:
        state, payload = db.execute(
            "SELECT state,payload FROM public.rag_video_jobs WHERE id=%s", (ids[0],)
        ).fetchone()
    assert state == "queued" and payload["source_url"] == source
    assert not payload.get("asset_id") and not payload.get("media_expired")
    assert payload["recovery_history"][-1]["asset_id"] == "b" * 32
