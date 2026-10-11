"""Bounded, resumable discovery of every followed author's published notes."""

from __future__ import annotations

import fcntl
import re
import time

from .config import ConfigError
from .video_provider import ProviderUnavailable
from .video_store import enqueue_url, ingestion_collection
from .xhs_favorites import normalize_page
from .xhs_following_client import FollowingClient
from .xhs_following_store import FollowingStore
from .xhs_private import private_directory, private_open

GLOBAL_ERRORS = {
    "XHS_LOGIN_REQUIRED",
    "XHS_LOGIN_OR_VERIFICATION_REQUIRED",
    "XHS_RATE_LIMITED",
    "XHS_ACCOUNT_CHANGED_RESTART_REQUIRED",
    "XHS_FAVORITES_API_REJECTED",
    "XHS_SOURCE_BROWSER_MISSING",
    "XHS_FAVORITES_SCHEMA_CHANGED",
    "XHS_FAVORITES_CURSOR_INVALID",
    "XHS_FOLLOWING_SCHEMA_CHANGED",
}


def error_code(exc):
    value = (
        (exc.code or str(exc))
        if isinstance(exc, ProviderUnavailable)
        else "XHS_FOLLOWING_SYNC_FAILED"
    )
    return (
        value
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,100}", value)
        else "XHS_FOLLOWING_SYNC_FAILED"
    )


def following_status(config, video, collection="all"):
    return FollowingStore(
        config, video, ingestion_collection(config, video, collection)
    ).status()


def following_control(config, video, paused, collection="all"):
    return FollowingStore(
        config, video, ingestion_collection(config, video, collection)
    ).control(paused)


def sync_following(
    config,
    video,
    collection="all",
    *,
    max_pages=0,
    restart=False,
    continue_only=False,
    seconds=1800,
    high_water=500,
    low_water=250,
    client=None,
    store=None,
    submit=None,
):
    collection = ingestion_collection(config, video, collection)
    if config.collection(collection).read_only:
        raise ConfigError("Selected collection is read-only.")
    if (
        not 0 <= max_pages <= 10000
        or not 1 <= seconds <= 86400
        or not 0 <= low_water < high_water
    ):
        raise ConfigError("Invalid following discovery limits.")
    store = store or FollowingStore(config, video, collection)
    submit = submit or enqueue_url
    root = video.root / "following"
    private_directory(root)
    with private_open(root / "scan.lock", append=True) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "already_running", "discovery_complete": False}
        if store.paused():
            return {**store.status(), "state": "paused"}
        previous = store.latest()
        if continue_only and (
            not previous
            or previous["state"] not in {"partial", "running", "backpressure"}
        ):
            return {**store.status(), "continuation_skipped": True}
        deadline = time.monotonic() + seconds
        scan_id = previous["scan_id"] if previous else None
        try:
            client = client or FollowingClient(video)
            owner = client.owner()
            store.bind(owner)
            snapshot = client.following(owner)
            scan_id, mode = store.snapshot(
                snapshot, restart=restart, refresh_heads=not continue_only
            )
            if (
                previous
                and previous["state"] == "backpressure"
                and store.queue_count() > low_water
            ):
                return store.finish(scan_id, "backpressure")
            pages = 0
            while time.monotonic() < deadline and (not max_pages or pages < max_pages):
                if store.paused():
                    return store.finish(scan_id, "paused")
                author = store.next_author(scan_id)
                if not author:
                    report = store.status()
                    if report["authors"].get("blocked", 0):
                        return store.finish(
                            scan_id, "blocked", "XHS_AUTHOR_SCAN_BLOCKED"
                        )
                    return store.finish(scan_id, "complete")
                if store.queue_count() >= high_water:
                    return store.finish(scan_id, "backpressure")
                author_id, checkpoint = author
                try:
                    head = checkpoint.get("head_pending", False)
                    current_cursor = (
                        checkpoint.get("head_cursor", "")
                        if head
                        else checkpoint["cursor"]
                    )
                    notes, more, cursor = normalize_page(
                        client.author_page(author_id, current_cursor)
                    )
                    seen = set(
                        checkpoint.get("head_seen" if head else "seen_cursors", [])
                    ) | {current_cursor}
                    if more and cursor in seen:
                        raise ProviderUnavailable("XHS_AUTHOR_CURSOR_REPEATED")
                    all_known = bool(notes)
                    # A page is the checkpoint unit. If interrupted halfway through,
                    # replay it; the queue and source relation are idempotent.
                    for note in notes:
                        if store.paused():
                            return store.finish(scan_id, "paused")
                        if time.monotonic() >= deadline:
                            return store.finish(scan_id, "partial")
                        if store.queue_count() >= high_water:
                            return store.finish(scan_id, "backpressure")
                        all_known = store.known(note["note_id"]) and all_known
                        job = submit(
                            config,
                            video,
                            note["url"],
                            collection,
                            note["title"],
                            source_note_id=note["note_id"],
                            allow_expired=False,
                            source_origin="following",
                            source_author_id=author_id,
                            source_page_cursor=current_cursor,
                        )
                        checkpoint[
                            "duplicates" if job.get("duplicate") else "submitted"
                        ] += 1
                    known_pages = (
                        checkpoint.get("head_known_pages" if head else "known_pages", 0)
                        + 1
                        if all_known
                        else 0
                    )
                    incremental_end = (
                        head
                        or (mode == "incremental" and not checkpoint.get("history"))
                    ) and known_pages >= 3
                    checkpoint["pages"] = checkpoint.get("pages", 0) + 1
                    if head:
                        checkpoint.update(
                            head_cursor=cursor,
                            head_seen=sorted(seen | {cursor}),
                            head_known_pages=known_pages,
                            head_pending=more and not incremental_end,
                        )
                        done = not checkpoint["head_pending"] and checkpoint.get(
                            "terminal", False
                        )
                    else:
                        checkpoint.update(
                            cursor=cursor,
                            seen_cursors=sorted(seen | {cursor}),
                            known_pages=known_pages,
                            terminal=not more,
                            incremental_end=incremental_end,
                        )
                        done = not more or incremental_end
                    store.save_author(
                        scan_id,
                        author_id,
                        checkpoint,
                        "complete" if done else "pending",
                    )
                    pages += 1
                except ProviderUnavailable as exc:
                    code = error_code(exc)
                    store.save_author(scan_id, author_id, checkpoint, "blocked", code)
                    if code in GLOBAL_ERRORS:
                        return store.finish(scan_id, "blocked", code)
            if not store.next_author(scan_id):
                if store.status()["authors"].get("blocked", 0):
                    return store.finish(scan_id, "blocked", "XHS_AUTHOR_SCAN_BLOCKED")
                return store.finish(scan_id, "complete")
            return store.finish(scan_id, "partial")
        except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - persist sanitized discovery failures
            code = (
                "XHS_FOLLOWING_SYNC_INTERRUPTED"
                if isinstance(exc, KeyboardInterrupt)
                else error_code(exc)
            )
            if scan_id:
                if code == "XHS_SESSION_BUSY":
                    return store.finish(scan_id, "partial", code)
                return store.finish(scan_id, "blocked", code)
            return {
                "state": "blocked",
                "error_code": code,
                "discovery_complete": False,
                "processing_complete": False,
            }
