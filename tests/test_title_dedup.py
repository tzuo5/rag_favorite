from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from test_knowledge_migration import live_library as isolated_library

from rag_favorite.database import connect_database
from rag_favorite.title_dedup import (
    backfill,
    canonical_job,
    deduplicate_job,
    title_text,
)
from rag_favorite.video_store import (
    enqueue_external_source,
    enqueue_reprocess,
    library_id,
)

live_library = isolated_library


@pytest.mark.parametrize(
    "value",
    [
        "",
        "无标题",
        "untitled",
        "a" * 24,
        "[00-00 - 00-01]",
        "[00:00 - 00:01]",
        "[01:00:03–01:00:06]",
    ],
)
def test_unknown_and_subtitle_placeholder_are_not_title_identities(value):
    assert title_text(value) == ""


def test_titles_keep_episode_numbers_and_only_remove_known_filename_suffix():
    assert title_text("  ＡＰＩ\n介绍  ") == "API 介绍"
    assert title_text("视频--abcdef12") == "视频--abcdef12"
    assert title_text("视频--abcdef12", legacy_filename=True) == "视频"
    assert title_text("第1集") != title_text("第2集")
    assert title_text("步骤-1") != title_text("步骤1")
    assert title_text("[00-00 - 00-01]--abcdef12", legacy_filename=True) == ""


def insert_job(
    config, video, title, *, state="queued", migration=None, supersedes=None
):
    ident = str(uuid4())
    payload = {
        "title": title,
        "source_url": "https://www.youtube.com/watch?v=abcdefghijk",
    }
    if migration:
        payload["migration_id"] = migration
    if supersedes:
        payload["supersedes"] = supersedes
    with connect_database(config) as c:
        c.execute(
            "INSERT INTO public.rag_video_jobs(id,library_id,collection,state,payload,title_key) VALUES(%s,%s,'general',%s,%s,%s)",
            (ident, library_id(video), state, Jsonb(payload), title_text(title)),
        )
    return {"id": ident, "payload": payload, "state": state, "collection": "general"}


def test_live_same_title_different_urls_reuses_task_and_keeps_sources(live_library):
    config, video = live_library
    first = enqueue_external_source(
        config, video, "https://youtu.be/abcdefghijk", title="同一标题"
    )
    second = enqueue_external_source(
        config, video, "https://youtu.be/12345678901", title="同一标题"
    )
    assert first["job_id"] == second["job_id"] and second["dedup_reason"] == "title"
    third = enqueue_external_source(
        config, video, "https://youtu.be/12345678902", title="同一标题 第2集"
    )
    assert third["job_id"] != first["job_id"]
    with connect_database(config) as c:
        payload = c.execute(
            "SELECT payload FROM public.rag_video_jobs WHERE id=%s", (first["job_id"],)
        ).fetchone()[0]
    assert payload["source_url"].endswith("abcdefghijk")
    assert payload["same_title_submissions"][0]["source_url"].endswith("12345678901")


def test_live_unknown_titles_are_independent_until_source_discovery(live_library):
    config, video = live_library
    a = enqueue_external_source(config, video, "https://youtu.be/12345678901")
    b = enqueue_external_source(
        config, video, "https://youtu.be/12345678902", title="[00:00 - 00:01]"
    )
    assert a["job_id"] != b["job_id"]
    completed = insert_job(config, video, "来源真实标题", state="complete")
    job = {"id": a["job_id"], "payload": {"title": "来源真实标题"}}
    assert deduplicate_job(config, video, job)
    with connect_database(config) as c:
        assert canonical_job(c, video, a["job_id"]) == completed["id"]
        assert (
            c.execute(
                "SELECT state FROM public.rag_video_jobs WHERE id=%s", (a["job_id"],)
            ).fetchone()[0]
            == "duplicate"
        )
    # An exact-source re-submission resolves the alias, even without a title.
    assert (
        enqueue_external_source(config, video, "https://youtu.be/12345678901")["job_id"]
        == completed["id"]
    )


def test_live_refresh_bypasses_old_complete_and_merges_superseded_documents(
    live_library,
):
    from rag_favorite.unified_library import register_document

    config, video = live_library
    old = insert_job(config, video, "同一标题", state="complete")
    ids = [str(uuid4()), str(uuid4())]
    for ident in ids:
        register_document(
            config,
            video,
            ident,
            "Original.",
            "同一标题--abcdef12",
            "old source",
            {
                "source_paths": ["old.md"],
                "source_urls": ["https://youtu.be/12345678901"],
            },
        )
    first = enqueue_reprocess(
        config, video, "https://youtu.be/12345678901", "refresh", [ids[0]]
    )
    with connect_database(config) as c:
        inherited = c.execute(
            "SELECT payload->'supersedes' FROM public.rag_video_jobs WHERE id=%s",
            (first["job_id"],),
        ).fetchone()[0]
    assert set(inherited) == set(ids)
    second = enqueue_reprocess(
        config, video, "https://youtu.be/12345678902", "refresh", [ids[1]]
    )
    assert first["job_id"] != old["id"] and first["job_id"] == second["job_id"]
    with connect_database(config) as c:
        payload = c.execute(
            "SELECT payload FROM public.rag_video_jobs WHERE id=%s", (first["job_id"],)
        ).fetchone()[0]
    assert (
        set(payload["supersedes"]) == set(ids)
        and first["job_id"] not in payload["supersedes"]
    )


def test_live_backfill_keeps_refresh_and_running_work_reversible_and_idempotent(
    live_library, tmp_path
):
    config, video = live_library
    # No source URL, so each explicit title retains its own identity in this fixture.
    old = insert_job(config, video, "A", state="complete")
    fresh = insert_job(config, video, "A", migration="refresh", supersedes=[old["id"]])
    redundant = insert_job(
        config, video, "A", migration="refresh", supersedes=[str(uuid4())]
    )
    running = insert_job(config, video, "B", state="running")
    waiting = insert_job(config, video, "B")
    with connect_database(config) as c:
        c.execute(
            "UPDATE public.rag_video_jobs SET payload=payload-'source_url' WHERE library_id=%s",
            (library_id(video),),
        )
    run = tmp_path / "snapshot"
    dry = backfill(config, video, run)
    assert dry["queued_tasks_merged"] == 2 and not run.exists()
    applied = backfill(config, video, run, apply=True)
    assert applied["queued_tasks_merged"] == 2
    snapshot = (run / "before.json").read_bytes()
    with connect_database(config) as c:
        assert canonical_job(c, video, redundant["id"]) == fresh["id"]
        assert canonical_job(c, video, waiting["id"]) == running["id"]
        states = dict(
            c.execute(
                "SELECT id::text,state FROM public.rag_video_jobs WHERE library_id=%s",
                (library_id(video),),
            ).fetchall()
        )
    assert states[old["id"]] == "complete" and states[running["id"]] == "running"
    assert backfill(config, video, run, apply=True)["queued_tasks_merged"] == 0
    assert (run / "before.json").read_bytes() == snapshot


def test_live_top_five_are_distinct_titles_and_merged_originals_remain_readable(
    live_library, monkeypatch
):
    from rag_favorite import unified_library as u
    from rag_favorite.indexes import resolve_index
    from rag_favorite.knowledge_summaries import text_hash

    config, video = live_library
    dimension = resolve_index(config).embedding.dimensions

    class Encoder:
        def embed_documents(self, texts):
            return [[1.0] + [0.0] * (dimension - 1) for _ in texts]

        def embed_query(self, query):
            return [1.0] + [0.0] * (dimension - 1)

    encoder = Encoder()
    monkeypatch.setattr(u, "client_from_config", lambda _: encoder)
    u.create_generation(config, video, "titles")
    ids = []
    for ordinal, title in enumerate(["A", "A", "A", "B", "C", "D", "E"]):
        ident = str(uuid4())
        ids.append(ident)
        u.register_document(
            config,
            video,
            ident,
            f"Original {ordinal}.",
            title,
            f"source {ordinal}",
            {"source_urls": [f"https://youtu.be/1234567890{ordinal}"]},
        )
        u.queue_summary(config, video, "titles", ident)
        u.publish_summary(
            config, video, "titles", ident, f"Summary {ordinal}.", encoder=encoder
        )
    u.activate_generation(config, video, "titles", ids)
    found = u.search(config, video, "Original", 5)
    assert len(found) == 5 and {r["title"] for r in found} == {"A", "B", "C", "D", "E"}
    selected = next(r for r in found if r["title"] == "A")
    peers = selected["metadata"]["same_title_documents"]
    assert len(peers) == 2
    for peer in peers:
        page = u.document_context(config, video, peer["document_id"])
        assert text_hash(page["original_document"]) == page["draft_sha256"]
    assert u.status(config, video)["published_documents"] == 5


def test_worker_discovers_title_before_media_download(live_library, monkeypatch):
    from rag_favorite import video_sources, video_worker

    config, video = live_library
    original = insert_job(config, video, "真实标题", state="complete")
    incoming = insert_job(config, video, "", state="running")
    incoming["payload"]["source_url"] = "https://youtu.be/12345678901"
    monkeypatch.setattr(video_sources, "fetch_external_title", lambda *_: "真实标题")
    monkeypatch.setattr(
        video_sources,
        "download_external_video",
        lambda *_: pytest.fail("Duplicate must not download media"),
    )
    monkeypatch.setattr(
        video_worker,
        "prepare_transcript",
        lambda *_: pytest.fail("Duplicate must not transcribe"),
    )
    video_worker.process(config, video, incoming)
    with connect_database(config) as c:
        assert canonical_job(c, video, incoming["id"]) == original["id"]


def test_external_title_adapter_is_metadata_only(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace

    from rag_favorite.video_config import VideoConfig
    from rag_favorite.video_sources import fetch_external_title

    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0, stdout='{"title":"真实视频标题","url":"private"}'
        )

    monkeypatch.setattr(subprocess, "run", run)
    video = VideoConfig(root=tmp_path, credentials_file=tmp_path / "missing")
    assert fetch_external_title(video, "https://youtu.be/abcdefghijk") == "真实视频标题"
    assert "--skip-download" in calls[0] and "--dump-single-json" in calls[0]
