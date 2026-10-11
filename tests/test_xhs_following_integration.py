"""Explicit PostgreSQL acceptance; isolated library, no live model requests."""

import os
from dataclasses import replace

import pytest

from rag_favorite.config import load_config
from rag_favorite.database import connect_database
from rag_favorite.video_config import load_video_config
from rag_favorite.video_provider import ProviderUnavailable
from rag_favorite.video_store import enqueue_url, library_id, stage_asset
from rag_favorite.xhs_following_store import FollowingStore
from rag_favorite.xhs_images import enqueue_live_photos

pytestmark = pytest.mark.skipif(
    not os.environ.get("RAG_FAVORITE_CONFIG") or not os.environ.get("RAG_VIDEO_CONFIG"),
    reason="Explicit private PostgreSQL/video profiles required",
)
A, B, OWNER = "a" * 24, "b" * 24, "c" * 24


@pytest.fixture
def library(tmp_path):
    config = load_config()
    video = replace(load_video_config(), root=tmp_path / "library")
    collection = next(k for k, v in config.collections.items() if not v.read_only)
    store = FollowingStore(config, video, collection)
    yield config, video, collection, store
    with connect_database(config) as c:
        c.execute(
            "DELETE FROM public.rag_xhs_sources WHERE library_id=%s",
            (library_id(video),),
        )
        c.execute(
            "DELETE FROM public.rag_xhs_scans WHERE library_id=%s", (library_id(video),)
        )
        c.execute(
            "DELETE FROM public.rag_xhs_authors WHERE library_id=%s",
            (library_id(video),),
        )
        c.execute(
            "DELETE FROM public.rag_xhs_accounts WHERE library_id=%s",
            (library_id(video),),
        )
        c.execute(
            "DELETE FROM public.rag_video_jobs WHERE library_id=%s",
            (library_id(video),),
        )


def snapshot(*authors):
    return {
        "complete": True,
        "count": len(authors),
        "authors": [{"author_id": a, "nickname": "author"} for a in authors],
    }


def test_three_origins_share_one_job_and_keep_all_relations(library):
    config, video, collection, _ = library
    url = "https://www.xiaohongshu.com/explore/" + A
    jobs = [
        enqueue_url(
            config,
            video,
            url + "?xsec_token=" + origin,
            collection,
            source_origin=origin,
            source_author_id=B if origin == "following" else "",
            allow_expired=False,
        )
        for origin in ("manual", "favorites", "following")
    ]
    assert len({j["job_id"] for j in jobs}) == 1
    with connect_database(config) as c:
        assert (
            c.execute(
                "SELECT count(*) FROM public.rag_xhs_sources WHERE library_id=%s",
                (library_id(video),),
            ).fetchone()[0]
            == 3
        )


def test_incomplete_snapshot_preserves_followed_authors_and_complete_removal_cancels(
    library,
):
    config, video, _, store = library
    store.bind(OWNER)
    scan, _ = store.snapshot(snapshot(A, B))
    store.finish(scan, "partial")
    with pytest.raises(ProviderUnavailable):
        store.snapshot({"complete": False, "count": 0, "authors": []})
    with connect_database(config) as c:
        assert (
            c.execute(
                "SELECT count(*) FROM public.rag_xhs_authors WHERE library_id=%s AND active",
                (library_id(video),),
            ).fetchone()[0]
            == 2
        )
    resumed, _ = store.snapshot(snapshot(A))
    assert resumed == scan
    assert store.status()["authors"]["cancelled"] == 1
    assert store.next_author(scan)[0] == A


def test_sql_checkpoint_and_owner_binding_survive_new_store(library):
    config, video, collection, store = library
    store.bind(OWNER)
    scan, _ = store.snapshot(snapshot(A))
    checkpoint = {
        "cursor": "next",
        "seen_cursors": ["next"],
        "pages": 1,
        "submitted": 0,
        "duplicates": 0,
    }
    store.save_author(scan, A, checkpoint)
    store.finish(scan, "partial")
    another = FollowingStore(config, video, collection)
    another.bind(OWNER)
    assert another.next_author(scan)[1] == checkpoint
    with pytest.raises(ProviderUnavailable, match="ACCOUNT_CHANGED"):
        another.bind(B)
    assert another.control(True)["configured"] and another.paused()
    assert another.control(False)["configured"] and not another.paused()


def test_empty_account_discovery_does_not_claim_processing_before_complete(library):
    _, _, _, store = library
    store.bind(OWNER)
    scan, _ = store.snapshot(snapshot())
    assert not store.status()["processing_complete"]
    result = store.finish(scan, "complete")
    assert result["discovery_complete"] and result["processing_complete"]


def test_daily_refresh_reopens_head_without_discarding_history(library):
    _, _, _, store = library
    store.bind(OWNER)
    scan, _ = store.snapshot(snapshot(A, B))
    store.save_author(
        scan, A, {"cursor": "history-next", "pages": 1, "terminal": False}
    )
    store.save_author(scan, B, {"cursor": "", "pages": 1, "terminal": True}, "complete")
    store.finish(scan, "partial")
    assert store.snapshot(snapshot(A, B), refresh_heads=True)[0] == scan
    with connect_database(store.config) as c:
        rows = dict(
            c.execute(
                "SELECT author_id,checkpoint FROM public.rag_xhs_author_scans WHERE scan_id=%s",
                (scan,),
            ).fetchall()
        )
    assert rows[A]["cursor"] == "history-next" and rows[A]["head_pending"]
    assert rows[B]["terminal"] and rows[B]["head_pending"]
    assert store.status()["authors"] == {"pending": 2}


def test_old_text_only_note_upgrades_once_without_duplicate_job(library):
    config, video, collection, _ = library
    url = "https://www.xiaohongshu.com/explore/" + A
    first = enqueue_url(config, video, url, collection)
    with connect_database(config) as c:
        c.execute(
            "UPDATE public.rag_video_jobs SET state='complete',stage='text_only' WHERE id=%s",
            (first["job_id"],),
        )
    upgraded = enqueue_url(
        config, video, url, collection, source_origin="following", allow_expired=False
    )
    assert upgraded["job_id"] == first["job_id"] and upgraded["state"] == "queued"
    with connect_database(config) as c:
        c.execute(
            "UPDATE public.rag_video_jobs SET state='complete',stage='text_only',payload=jsonb_set(payload,'{image_note_version}','1') WHERE id=%s",
            (first["job_id"],),
        )
    assert (
        enqueue_url(
            config,
            video,
            url,
            collection,
            source_origin="following",
            allow_expired=False,
        )["state"]
        == "complete"
    )


def test_live_photo_child_is_idempotent_and_affects_processing_status(
    library, tmp_path
):
    config, video, collection, store = library
    store.bind(OWNER)
    scan, _ = store.snapshot(snapshot(B))
    url = "https://www.xiaohongshu.com/explore/" + A
    parent = enqueue_url(
        config, video, url, collection, source_origin="following", source_author_id=B
    )
    note = {
        "note_id": A,
        "author_id": B,
        "title": "live",
        "images": [
            {
                "image_index": 0,
                "live_photo": True,
                "live_video_url": "https://sns-video.xhscdn.com/test",
            }
        ],
    }
    downloads = []

    def download(v, n):
        downloads.append(n)
        path = tmp_path / "live.mp4"
        path.write_bytes(b"test-owned-asset")
        return stage_asset(path, v)

    job = {"collection": collection, "payload": {"source_url": url}}
    first = enqueue_live_photos(config, video, note, job, downloader=download)
    assert enqueue_live_photos(config, video, note, job, downloader=download) == first
    assert len(downloads) == 1
    with connect_database(config) as c:
        c.execute(
            "UPDATE public.rag_video_jobs SET state='complete' WHERE id=%s",
            (parent["job_id"],),
        )
    result = store.finish(scan, "complete")
    assert result["jobs"] == {"complete": 1, "queued": 1}
    assert not result["processing_complete"]
