import hashlib
from types import SimpleNamespace

import pytest

from rag_favorite import xhs_refresh
from rag_favorite.video_provider import ProviderUnavailable
from rag_favorite.xhs_private import atomic_private_json, private_directory


def test_refresh_is_bounded_to_one_originating_page_and_one_retry(
    tmp_path, monkeypatch
):
    root = tmp_path / "favorites"
    private_directory(root)
    owner, author, nid = "a" * 24, "b" * 24, "c" * 24
    atomic_private_json(
        root / "owner.json", {"owner_hash": hashlib.sha256(owner.encode()).hexdigest()}
    )
    calls = []
    writes = []

    class Database:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, *args):
            writes.append(args)

    monkeypatch.setattr(xhs_refresh, "connect_database", lambda c: Database())
    monkeypatch.setattr(xhs_refresh, "library_id", lambda v: "library")
    client = SimpleNamespace(
        owner=lambda: owner,
        author_page=lambda a, cursor: {
            "notes": [{"note_id": nid, "xsec_token": "new"}],
            "has_more": False,
        },
    )
    job = {
        "id": "job",
        "payload": {
            "source_url": "https://www.xiaohongshu.com/explore/"
            + nid
            + "?xsec_token=old",
            "source_note_id": nid,
            "source_author_id": author,
            "source_page_cursor": "saved-page",
        },
    }

    def fetch(video, url):
        calls.append(url)
        if len(calls) == 1:
            raise ProviderUnavailable("XHS_LOGIN_EXPIRED_OR_NOTE_UNAVAILABLE")
        return {"note_id": nid}

    assert xhs_refresh.fetch_with_refresh(
        None, SimpleNamespace(root=tmp_path), job, fetcher=fetch, client=client
    ) == {"note_id": nid}
    assert len(calls) == 2 and "new" in calls[-1] and len(writes) == 1


def test_refresh_never_retries_a_login_failure():
    def fetch(video, url):
        raise ProviderUnavailable("XHS_LOGIN_REQUIRED")

    client = SimpleNamespace(
        owner=lambda: pytest.fail("Login failures cannot refresh automatically")
    )
    with pytest.raises(ProviderUnavailable, match="LOGIN_REQUIRED"):
        xhs_refresh.fetch_with_refresh(
            None,
            None,
            {"payload": {"source_url": "url", "source_author_id": "a" * 24}},
            fetcher=fetch,
            client=client,
        )


def test_refresh_restarts_from_author_first_page_when_saved_cursor_moves(
    tmp_path, monkeypatch
):
    root = tmp_path / "favorites"
    private_directory(root)
    owner, nid = "a" * 24, "c" * 24
    atomic_private_json(
        root / "owner.json", {"owner_hash": hashlib.sha256(owner.encode()).hexdigest()}
    )
    pages, fetched = [], []

    class Database:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, *args):
            pass

    monkeypatch.setattr(xhs_refresh, "connect_database", lambda c: Database())
    monkeypatch.setattr(xhs_refresh, "library_id", lambda v: "library")

    def author_page(author, cursor):
        pages.append(cursor)
        return {
            "notes": [] if cursor else [{"note_id": nid, "xsec_token": "new"}],
            "has_more": False,
        }

    def fetch(video, url):
        fetched.append(url)
        if len(fetched) == 1:
            raise ProviderUnavailable("XHS_SOURCE_UNAVAILABLE")
        return {"note_id": nid}

    job = {
        "id": "job",
        "payload": {
            "source_url": "old",
            "source_note_id": nid,
            "source_author_id": "b" * 24,
            "source_page_cursor": "obsolete",
        },
    }
    result = xhs_refresh.fetch_with_refresh(
        None,
        SimpleNamespace(root=tmp_path),
        job,
        fetcher=fetch,
        client=SimpleNamespace(owner=lambda: owner, author_page=author_page),
    )
    assert result["note_id"] == nid and pages == ["obsolete", ""]
