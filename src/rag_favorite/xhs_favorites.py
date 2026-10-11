"""Owner-only signed collection pagination into the durable ingestion queue.

Endpoint reference: https://github.com/ReaJason/xhs/blob/master/xhs/core.py
No account credentials or signed URLs are returned to callers.
"""

from __future__ import annotations

import fcntl
import hashlib
import re
import time
from datetime import UTC, datetime
from urllib.parse import urlencode

from .config import ConfigError
from .video_provider import ProviderUnavailable
from .video_store import enqueue_url
from .xhs_client import XhsSessionClient as FavoritesClient
from .xhs_private import (
    atomic_private_json,
    private_directory,
    private_json,
    private_open,
)

NOTE_ID = re.compile(r"^[a-f0-9]{24}$")


def state_path(video, collection):
    root = video.root / "favorites"
    return root / (hashlib.sha256(collection.encode()).hexdigest() + ".json")


def save_state(path, state):
    state["updated_at"] = datetime.now(UTC).isoformat()
    atomic_private_json(path, state)


def favorites_status(video, collection):
    path = state_path(video, collection)
    return (
        private_json(path)
        if path.exists() or path.is_symlink()
        else {"state": "not_started"}
    )


def normalize_page(data):
    if not isinstance(data, dict):
        raise ProviderUnavailable("XHS_FAVORITES_SCHEMA_CHANGED")
    notes = data.get("notes")
    more = data.get("has_more")
    if not isinstance(notes, list) or len(notes) > 100 or not isinstance(more, bool):
        raise ProviderUnavailable("XHS_FAVORITES_SCHEMA_CHANGED")
    result = []
    for note in notes:
        if not isinstance(note, dict):
            raise ProviderUnavailable("XHS_FAVORITES_SCHEMA_CHANGED")
        note_id = note.get("note_id") or note.get("id")
        if not isinstance(note_id, str) or not NOTE_ID.fullmatch(note_id):
            raise ProviderUnavailable("XHS_FAVORITES_SCHEMA_CHANGED")
        token = note.get("xsec_token") or note.get("xsecToken") or ""
        if not isinstance(token, str) or len(token) > 4000:
            raise ProviderUnavailable("XHS_FAVORITES_SCHEMA_CHANGED")
        query = (
            urlencode({"xsec_token": token, "xsec_source": "pc_user"}) if token else ""
        )
        result.append(
            {
                "note_id": note_id,
                "title": str(note.get("display_title") or note.get("title") or "")[
                    :300
                ],
                "url": "https://www.xiaohongshu.com/explore/"
                + note_id
                + ("?" + query if query else ""),
            }
        )
    cursor = data.get("cursor", "")
    if (
        not isinstance(cursor, str)
        or len(cursor) > 4000
        or (more and (not cursor or not notes))
    ):
        raise ProviderUnavailable("XHS_FAVORITES_CURSOR_INVALID")
    return result, more, cursor


def sync_favorites(
    config, video, collection, *, max_pages=0, restart=False, client=None, pause=2
):
    if config.collection(collection).read_only:
        raise ConfigError("Selected collection is read-only.")
    if not 0 <= max_pages <= 10000 or not 0 <= pause <= 60:
        raise ConfigError("Invalid page limit.")
    path = state_path(video, collection)
    private_directory(path.parent)
    with private_open(path.with_suffix(".lock"), append=True) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "already_running"}
        previous = favorites_status(video, collection)
        state = (
            previous
            if not restart
            and previous.get("state") in {"blocked", "partial", "running"}
            else {}
        )
        state = {
            "state": "running",
            "cursor": "",
            "pages": 0,
            "submitted": 0,
            "duplicates": 0,
            **state,
        }
        state.update(state="running", error_code=None)
        save_state(path, state)
        try:
            client = client or FavoritesClient(video)
            owner = client.owner()
            owner_hash = hashlib.sha256(owner.encode()).hexdigest()
            owner_path = path.parent / "owner.json"
            binding = private_json(owner_path) if owner_path.exists() else {}
            if binding.get("owner_hash") not in {None, owner_hash} or previous.get(
                "owner_hash"
            ) not in {None, owner_hash}:
                raise ProviderUnavailable("XHS_ACCOUNT_CHANGED_RESTART_REQUIRED")
            if not binding:
                with private_open(
                    path.parent / "owner.lock", append=True
                ) as owner_lock:
                    fcntl.flock(owner_lock, fcntl.LOCK_EX)
                    binding = private_json(owner_path) if owner_path.exists() else {}
                    if binding.get("owner_hash") not in {None, owner_hash}:
                        raise ProviderUnavailable(
                            "XHS_ACCOUNT_CHANGED_RESTART_REQUIRED"
                        )
                    save_state(owner_path, {"owner_hash": owner_hash})
            state["owner_hash"] = owner_hash
            save_state(path, state)
            seen_cursors = set(state.get("seen_cursors", [])) | {state["cursor"]}
            for _ in range(max_pages or 10000):
                notes, more, cursor = normalize_page(
                    client.page(owner, state["cursor"])
                )
                for note in notes:
                    job = enqueue_url(
                        config,
                        video,
                        note["url"],
                        collection,
                        note["title"],
                        source_note_id=note["note_id"],
                        allow_expired=False,
                        source_origin="favorites",
                    )
                    state["duplicates" if job.get("duplicate") else "submitted"] += 1
                state["pages"] += 1
                if more and cursor in seen_cursors:
                    raise ProviderUnavailable("XHS_FAVORITES_CURSOR_REPEATED")
                seen_cursors.add(cursor)
                state.update(
                    cursor=cursor,
                    seen_cursors=list(seen_cursors),
                    state="running" if more else "complete",
                )
                save_state(path, state)
                if not more:
                    return state
                time.sleep(pause)
            state["state"] = "partial"
        except KeyboardInterrupt:
            state.update(state="blocked", error_code="XHS_FAVORITES_SYNC_INTERRUPTED")
        except Exception as exc:  # noqa: BLE001 - persist sanitized failure state
            state.update(
                state="blocked",
                error_code=(exc.code or str(exc))
                if isinstance(exc, ProviderUnavailable)
                and re.fullmatch(r"[A-Z][A-Z0-9_]{2,100}", exc.code or str(exc))
                else "XHS_FAVORITES_SYNC_FAILED",
            )
        save_state(path, state)
        return state
