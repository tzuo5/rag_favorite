"""Bounded refresh of the originating author's pages before media processing."""

from __future__ import annotations

import hashlib

from psycopg.types.json import Jsonb

from .database import connect_database
from .video_provider import ProviderUnavailable
from .video_sources import fetch_note
from .video_store import library_id
from .xhs_favorites import normalize_page
from .xhs_following_client import FollowingClient
from .xhs_private import private_json


def fetch_with_refresh(config, video, job, *, fetcher=None, client=None):
    fetcher = fetcher or fetch_note
    payload = job["payload"]
    try:
        return fetcher(video, payload["source_url"])
    except ProviderUnavailable as exc:
        if str(exc) not in {
            "XHS_LOGIN_EXPIRED_OR_NOTE_UNAVAILABLE",
            "XHS_SOURCE_UNAVAILABLE",
        } or not payload.get("source_author_id"):
            raise
    client = client or FollowingClient(video)
    owner = client.owner()
    binding = private_json(video.root / "favorites" / "owner.json")
    if binding.get("owner_hash") != hashlib.sha256(owner.encode()).hexdigest():
        raise ProviderUnavailable(
            "XHS_ACCOUNT_CHANGED_RESTART_REQUIRED",
            code="XHS_ACCOUNT_CHANGED_RESTART_REQUIRED",
        )
    cursor = payload.get("source_page_cursor", "")
    seen, target = set(), None
    for _ in range(5):
        if cursor in seen:
            break
        seen.add(cursor)
        notes, more, next_cursor = normalize_page(
            client.author_page(payload["source_author_id"], cursor)
        )
        target = next(
            (note for note in notes if note["note_id"] == payload["source_note_id"]),
            None,
        )
        if target:
            break
        # Newly posted notes can shift the saved cursor. Check the same
        # author's first page once before following its current pagination.
        if "" not in seen:
            cursor = ""
        elif more:
            cursor = next_cursor
        else:
            break
    if target is None:
        raise ProviderUnavailable(
            "XHS_SOURCE_TOKEN_REFRESH_UNAVAILABLE",
            code="XHS_SOURCE_TOKEN_REFRESH_UNAVAILABLE",
        )
    payload["source_url"] = target["url"]
    with connect_database(config) as c:
        c.execute(
            "UPDATE public.rag_video_jobs SET payload=payload || %s WHERE id=%s AND library_id=%s AND state='running'",
            (Jsonb({"source_url": target["url"]}), job["id"], library_id(video)),
        )
    return fetcher(video, target["url"])
