from dataclasses import replace

import pytest

from rag_favorite.config import CollectionConfig, ConfigError, default_config
from rag_favorite.knowledge_migration import inspect_legacy, ocr_text
from rag_favorite.paths import AppPaths
from rag_favorite.unified_library import extractive_summary, retrieval_text
from rag_favorite.video_config import VideoConfig
from rag_favorite.video_sources import normalize_media_url
from rag_favorite.video_store import ingestion_collection


def test_bundle_dedup_retains_distinct_image_evidence(tmp_path):
    for folder, image in [("a", b"first"), ("b", b"second"), ("c", b"first")]:
        directory = tmp_path / folder
        directory.mkdir()
        (directory / "same.md").write_text(
            "# Recipe\n\n![[cover#1.jpg]]\n\nMix 2 eggs."
        )
        (directory / "cover#1.jpg").write_bytes(image)
    result = inspect_legacy(tmp_path)
    assert result["unique_documents"] == 2
    assert not result["unresolved_references"]
    assert not result["unreferenced_images"]
    assert result["documents"][0]["id"] == result["documents"][2]["id"]


def test_extensionless_obisidian_links_resolve_without_guessing(tmp_path):
    (tmp_path / "other.md").write_text("A source note.")
    (tmp_path / "note.md").write_text("[[other]] and [[missing]]")
    result = inspect_legacy(tmp_path)
    note = next(d for d in result["documents"] if d["title"] == "note")
    assert note["references"]["other"] == str(tmp_path / "other.md")
    assert result["unresolved_references"] == [
        {"source": "note.md", "reference": "missing"}
    ]


def test_cross_root_symlink_reference_is_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    external = tmp_path / "secret.md"
    external.write_text("outside")
    (root / "alias.md").symlink_to(external)
    (root / "note.md").write_text("[[alias.md]]")
    result = inspect_legacy(root)
    assert len(result["documents"]) == 1
    assert result["unresolved_references"]


def test_topic_and_source_metadata_never_enter_embedding_input():
    original = "# Actual title\n\n文档类型：烹饪\n来源：Cooking/archive\n标签：技术\n\nMix 2 eggs.\n"
    changed = (
        original.replace("烹饪", "财务")
        .replace("Cooking/archive", "Finance")
        .replace("技术", "文学")
    )
    assert retrieval_text(original) == retrieval_text(changed)
    assert "2 eggs" in retrieval_text(original)
    assert "来源" not in retrieval_text(original)
    assert (
        retrieval_text("---\ntags: private\n---\n# Actual title\n\nBody.")
        == "# Actual title\n\nBody.\n"
    )


def test_source_verified_summary_keeps_numbers_and_uncertainty():
    text = "# Title\n\nMix 2 eggs. Amount [unknown].\n\n## Conditions\n\nBake 10 minutes; temperature uncertain."
    summary = extractive_summary(text, "Title")
    for line in ["Mix 2 eggs.", "[unknown]", "10 minutes", "temperature uncertain"]:
        assert line in summary
    assert "原文段落" in summary
    with pytest.raises(ConfigError, match="NO_TEXT"):
        extractive_summary("# Image\n\n![Image](../attachments/a.jpg)", "Image")


def test_ocr_uncertainty_is_preserved():
    text = ocr_text(
        {
            "ocr": [
                {"text": "12 g", "confidence": 0.2},
                {"text": "stir", "confidence": 0.99},
            ]
        }
    )
    assert text == "12 g [unknown: OCR置信度低]\nstir"


@pytest.mark.parametrize(
    "source,key,url",
    [
        (
            "https://www.xiaohongshu.com/discovery/item/"
            + "a" * 24
            + "?xsec_token=abc",
            "xhs:" + "a" * 24,
            "https://www.xiaohongshu.com/explore/" + "a" * 24,
        ),
        (
            "https://youtu.be/abcdefghijk?t=3",
            "youtube:abcdefghijk",
            "https://www.youtube.com/watch?v=abcdefghijk",
        ),
        (
            "https://www.bilibili.com/video/BV1234/?spm_id_from=x",
            "bilibili:BV1234",
            "https://www.bilibili.com/video/BV1234",
        ),
    ],
)
def test_source_identity_ignores_access_and_tracking_tokens(source, key, url):
    assert normalize_media_url(source) == (key, url)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.youtube.com/watch?v=abcdefghijk",
        "https://localhost/video/1",
        "https://user:secret@www.youtube.com/watch?v=abcdefghijk",
        "https://www.youtube.com.evil.com/watch?v=abcdefghijk",
    ],
)
def test_untrusted_source_hosts_are_not_admitted(url):
    with pytest.raises(ConfigError):
        normalize_media_url(url)


def test_unified_legacy_aliases_cannot_route_or_filter_by_topic(tmp_path):
    config = default_config(AppPaths.discover())
    root = tmp_path / "knowledge"
    config = replace(
        config,
        unified=True,
        collections={"general": CollectionConfig("general", "Knowledge", root)},
    )
    video = VideoConfig(root=tmp_path / "data", credentials_file=tmp_path / "secrets")
    for alias in ["all", "cooking", "tech", "social-conduct", "general"]:
        assert ingestion_collection(config, video, alias) == "general"
        assert config.collection(alias).path == root
    with pytest.raises(ConfigError):
        config.collection("unrecognized")


@pytest.fixture
def live_library(tmp_path):
    import os

    from rag_favorite.config import load_config
    from rag_favorite.database import connect_database
    from rag_favorite.video_store import library_id

    path = os.environ.get("RAG_SUMMARY_TEST_CONFIG")
    if not path:
        pytest.skip("Set RAG_SUMMARY_TEST_CONFIG for PostgreSQL integration")
    config = replace(
        load_config(path),
        unified=True,
        collections={
            "general": CollectionConfig("general", "Knowledge", tmp_path / "knowledge")
        },
    )
    video = VideoConfig(root=tmp_path / "owned", credentials_file=tmp_path / "none")
    yield config, video
    lid = library_id(video)
    with connect_database(config) as c:
        for table in [
            "rag_legacy_reprocess",
            "rag_document_summary_jobs",
            "rag_document_summary_chunks",
            "rag_document_summaries",
            "rag_library_state",
            "rag_library_generations",
            "rag_knowledge_documents",
            "rag_video_jobs",
        ]:
            c.execute("DELETE FROM public." + table + " WHERE library_id=%s", (lid,))


def test_live_resume_repairs_task_and_summary_file_without_reembedding(live_library):
    from uuid import uuid4

    from rag_favorite import unified_library as u
    from rag_favorite.database import connect_database
    from rag_favorite.indexes import resolve_index
    from rag_favorite.video_store import library_id

    config, video = live_library
    ident = str(uuid4())
    dimension = resolve_index(config).embedding.dimensions

    class Encoder:
        def embed_documents(self, texts):
            return [[1.0] + [0.0] * (dimension - 1) for _ in texts]

    u.create_generation(config, video, "resume")
    u.register_document(config, video, ident, "Original facts.", "Title", "source", {})
    u.queue_summary(config, video, "resume", ident)
    summary = "# Title\n\nOriginal facts."
    u.publish_summary(config, video, "resume", ident, summary, encoder=Encoder())
    file = config.collection("general").path / ident / "summary.md"
    file.unlink()
    with connect_database(config) as c:
        c.execute(
            "UPDATE public.rag_document_summary_jobs SET state='failed',error_code='INTERRUPTED' WHERE document_id=%s",
            (ident,),
        )

    class NoReembedding:
        def embed_documents(self, texts):
            pytest.fail("Resuming unchanged knowledge must reuse its embeddings")

    result = u.publish_summary(
        config, video, "resume", ident, summary, encoder=NoReembedding()
    )
    assert result["cached"] and file.read_text() == summary
    with connect_database(config) as c:
        row = c.execute(
            "SELECT state,error_code FROM public.rag_document_summary_jobs WHERE library_id=%s AND document_id=%s",
            (library_id(video), ident),
        ).fetchone()
    assert row == ("complete", None)


def test_live_shadow_publication_alias_read_and_failed_rebuild_survives(
    live_library, monkeypatch
):
    from uuid import uuid4

    from rag_favorite import unified_library as u
    from rag_favorite.database import connect_database
    from rag_favorite.indexes import resolve_index

    config, video = live_library
    dimension = resolve_index(config).embedding.dimensions

    class Encoder:
        def embed_documents(self, texts):
            return [[1.0] + [0.0] * (dimension - 1) for t in texts]

        def embed_query(self, query):
            return [1.0] + [0.0] * (dimension - 1)

    encoder = Encoder()
    monkeypatch.setattr(u, "client_from_config", lambda _: encoder)
    ids = [str(uuid4()), str(uuid4())]
    u.create_generation(config, video, "test-shadow")
    for ident in ids:
        u.register_document(
            config,
            video,
            ident,
            "原文参数 12 克。\n" * 40,
            "Title",
            "source",
            {"document_type": "烹饪"},
        )
        u.queue_summary(config, video, "test-shadow", ident)
        u.publish_summary(
            config,
            video,
            "test-shadow",
            ident,
            "# Title\n\n文档类型：烹饪\n来源：Cooking\n\n12 克。",
            encoder=encoder,
        )
    with pytest.raises(ConfigError, match="missing"):
        u.activate_generation(config, video, "test-shadow", ids + [str(uuid4())])
    u.activate_generation(config, video, "test-shadow", ids)
    found = u.search(config, video, "12 克", 5)
    assert len(found) == 2 and all(r["knowledge_base"] == "general" for r in found)
    page = u.document_context(
        config,
        video,
        found[0]["document_id"],
        version=found[0]["metadata"]["version"],
        max_characters=20,
    )
    assert not page["complete"] and page["next_offset"] == 20
    with pytest.raises(ConfigError, match="VERSION_CHANGED"):
        u.document_context(config, video, found[0]["document_id"], version="wrong")
    with connect_database(config) as c:
        texts = c.execute(
            "SELECT search_text FROM public.rag_document_summaries WHERE document_id=ANY(%s::uuid[])",
            (ids,),
        ).fetchall()
    assert all("文档类型" not in t[0] and "来源" not in t[0] for t in texts)

    class FailingEncoder:
        def embed_documents(self, texts):
            raise RuntimeError("backend down")

    with pytest.raises(RuntimeError):
        u.publish_summary(
            config, video, "test-shadow", ids[0], "changed", encoder=FailingEncoder()
        )
    assert len(u.search(config, video, "12 克", 5)) == 2


def test_live_reprocess_creates_new_job_once_instead_of_skipping_completed(
    live_library,
):
    from uuid import uuid4

    from rag_favorite.database import connect_database
    from rag_favorite.video_store import enqueue_reprocess, library_id

    config, video = live_library
    old = str(uuid4())
    url = "https://www.youtube.com/watch?v=abcdefghijk"
    with connect_database(config) as c:
        c.execute(
            "INSERT INTO public.rag_video_jobs(id,library_id,collection,state,payload) VALUES(%s,%s,'cooking','complete','{}')",
            (old, library_id(video)),
        )
    first = enqueue_reprocess(config, video, url, "test-migration", ["legacy-doc"])
    second = enqueue_reprocess(config, video, url, "test-migration", ["legacy-doc"])
    assert first["job_id"] != old and first["job_id"] == second["job_id"]
    assert first["state"] == "queued" and second["duplicate"]
    with connect_database(config) as c:
        assert (
            c.execute(
                "SELECT state FROM public.rag_video_jobs WHERE id=%s", (old,)
            ).fetchone()[0]
            == "complete"
        )


def test_large_transcript_is_a_bounded_summary_not_a_second_fulltext_index():
    text = (
        "# Interview\n\n"
        + "Discuss API design, version "
        + "12.5. "
        + ("This is a source sentence about API design. " * 5000)
    )
    result = extractive_summary(text, "API design interview")
    assert len(result) < 4000
    assert "12.5" in result
    assert "原文段落" in result


@pytest.mark.parametrize("previous_pause", [False, True])
def test_roll_back_keeps_new_tasks_paused_and_preserves_owner_control(
    tmp_path, monkeypatch, previous_pause
):
    import json
    from contextlib import contextmanager
    from uuid import uuid4

    from rag_favorite import knowledge_migration as migration
    from rag_favorite.queue_control import read_control, set_control

    config = replace(
        default_config(AppPaths.discover()), source=tmp_path / "product.toml"
    )
    config.source.write_text('[index]\ngeneration="active"\n')
    video = VideoConfig(root=tmp_path / "owned", credentials_file=tmp_path / "none")
    owner_job = str(uuid4())
    new_job = str(uuid4())
    set_control(video, paused=previous_pause, job_id=owner_job, job_paused=True)
    source = tmp_path / "legacy"
    source.mkdir()

    class Runner:
        def __init__(self, *args):
            pass

        def apply(self):
            return ()

    class Connection:
        def execute(self, *args):
            return self

        def fetchone(self):
            return ("public.rag_legacy_reprocess",)

        def fetchall(self):
            return [(new_job,)]

    @contextmanager
    def database(*args, **kwargs):
        yield Connection()

    def fail(*args):
        raise RuntimeError("deliberate generation failure")

    monkeypatch.setattr(migration, "MigrationRunner", Runner)
    monkeypatch.setattr(migration, "wait_idle", lambda *args: None)
    monkeypatch.setattr(migration, "create_generation", fail)
    monkeypatch.setattr(migration, "connect_database", database)
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="deliberate"):
        migration.migrate(config, video, source, tmp_path / "run", apply=True)
    control = read_control(video)
    assert control.paused == previous_pause
    assert set(control.paused_jobs) == {owner_job, new_job}
    report = json.loads((tmp_path / "run/report.json").read_text())
    assert report["stage"] == "failed"
    assert json.loads((tmp_path / "run/migration-paused-jobs.json").read_text()) == [
        new_job
    ]


def test_legacy_h1_recipe_sections_keep_the_body_without_blank_lines():
    text = "# 原料\n- 200 g #豌豆 #peas\n- 60 ml 牛奶\n\n# 步骤\n- 把豌豆焯水 3 分钟。\n- 搅拌。"
    summary = extractive_summary(text, "豌豆泥")
    assert "200 g #豌豆" in summary
    assert "60 ml 牛奶" in summary
    assert "3 分钟" in summary
    assert "搅拌" in summary


def test_main_heading_followed_by_body_is_not_discarded():
    assert "actual knowledge" in extractive_summary(
        "# A title\nactual knowledge.", "A title"
    )
