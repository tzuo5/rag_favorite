"""Real PostgreSQL queue tests confined to a temporary library; no model calls."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from psycopg.types.json import Jsonb

from rag_favorite.config import ConfigError, load_config
from rag_favorite.database import connect_database
from rag_favorite.video_config import load_video_config
from rag_favorite.video_retention import retry_job
from rag_favorite.video_store import enqueue_url, library_id

pytestmark = pytest.mark.skipif(
    not os.environ.get("RAG_FAVORITE_CONFIG") or not os.environ.get("RAG_VIDEO_CONFIG"),
    reason="Explicit private PostgreSQL/video profiles required",
)


@pytest.fixture
def isolated_queue(tmp_path):
    config = load_config()
    video = replace(load_video_config(), root=tmp_path)
    collection = next(k for k, v in config.collections.items() if not v.read_only)
    yield config, video, collection
    with connect_database(config) as c:
        c.execute(
            "DELETE FROM public.rag_video_jobs WHERE library_id=%s",
            (library_id(video),),
        )


def test_note_id_dedup_between_manual_favorites_and_new_token(isolated_queue):
    config, video, collection = isolated_queue
    url = "https://www.xiaohongshu.com/explore/" + "b" * 24
    first = enqueue_url(config, video, url + "?xsec_token=old", collection)
    second = enqueue_url(
        config,
        video,
        url + "?xsec_token=new",
        collection,
        source_note_id="b" * 24,
        allow_expired=False,
    )
    assert second["duplicate"]
    assert second["job_id"] == first["job_id"]
    with connect_database(config) as c:
        stored = c.execute(
            "SELECT payload->>'source_url' FROM public.rag_video_jobs WHERE id=%s",
            (first["job_id"],),
        ).fetchone()[0]
    assert stored.endswith("=new")


def test_retry_time_budget_failure_with_zero_preserves_job_and_removes_deadline(
    isolated_queue,
):
    config, video, collection = isolated_queue
    first = enqueue_url(
        config, video, "https://www.xiaohongshu.com/explore/" + "b" * 24, collection
    )
    with connect_database(config) as c:
        c.execute(
            "UPDATE public.rag_video_jobs SET state='blocked',stage='failed:extract:4',error_code='VISUAL_TIME_BUDGET_EXCEEDED' WHERE id=%s AND library_id=%s",
            (first["job_id"], library_id(video)),
        )
    retry_job(config, video, first["job_id"], visual_seconds=0)
    with connect_database(config) as c:
        row = c.execute(
            "SELECT state,error_code,payload->>'visual_budget_seconds' FROM public.rag_video_jobs WHERE id=%s AND library_id=%s",
            (first["job_id"], library_id(video)),
        ).fetchone()
    assert row == ("queued", None, "0")


def test_nightly_does_not_redownload_expired_failure(isolated_queue):
    config, video, collection = isolated_queue
    url = "https://www.xiaohongshu.com/explore/" + "b" * 24
    first = enqueue_url(config, video, url, collection)
    with connect_database(config) as c:
        c.execute(
            "UPDATE public.rag_video_jobs SET state='blocked',payload=payload || %s WHERE id=%s",
            (Jsonb({"media_expired": True}), first["job_id"]),
        )
    auto = enqueue_url(config, video, url, collection, allow_expired=False)
    assert auto["duplicate"] and auto["job_id"] == first["job_id"]
    manual = enqueue_url(config, video, url, collection)
    assert manual["job_id"] != first["job_id"]


def test_concurrent_submissions_create_exactly_one_job(isolated_queue):
    config, video, collection = isolated_queue
    url = "https://www.xiaohongshu.com/explore/" + "b" * 24
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(
                lambda _: enqueue_url(
                    config, video, url, collection, allow_expired=False
                ),
                range(12),
            )
        )
    assert len({x["job_id"] for x in results}) == 1
    assert sum(not x.get("duplicate") for x in results) == 1


def test_url_and_note_id_must_agree(isolated_queue):
    config, video, collection = isolated_queue
    with pytest.raises(ConfigError):
        enqueue_url(
            config,
            video,
            "https://www.xiaohongshu.com/explore/" + "b" * 24,
            collection,
            source_note_id="c" * 24,
        )
