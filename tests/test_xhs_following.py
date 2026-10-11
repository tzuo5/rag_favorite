from copy import deepcopy
from types import SimpleNamespace

import pytest

from rag_favorite.video_provider import ProviderUnavailable
from rag_favorite.xhs_following import sync_following
from rag_favorite.xhs_following_client import normalize_following

A, B = "a" * 24, "b" * 24


def page(note, more=False, cursor=""):
    return {
        "notes": [{"note_id": note, "title": "note"}],
        "has_more": more,
        "cursor": cursor,
    }


class Store:
    def __init__(self):
        self.report = {"state": "not_started", "authors": {}}
        self.checkpoints = {}
        self.is_paused = False
        self.known_ids = set()
        self.queue = 0
        self.bound = None

    def bind(self, owner):
        if self.bound and self.bound != owner:
            raise ProviderUnavailable("XHS_ACCOUNT_CHANGED_RESTART_REQUIRED")
        self.bound = owner

    def paused(self):
        return self.is_paused

    def latest(self):
        return deepcopy(self.report) if self.report["state"] != "not_started" else None

    def snapshot(self, snapshot, restart=False, refresh_heads=False):
        if snapshot.get("complete") is not True:
            raise ProviderUnavailable("FOLLOWING_ENUMERATION_UNAVAILABLE")
        for a in snapshot["authors"]:
            if a["author_id"] not in self.checkpoints or restart:
                self.checkpoints[a["author_id"]] = (
                    "pending",
                    {
                        "cursor": "",
                        "seen_cursors": [],
                        "pages": 0,
                        "submitted": 0,
                        "duplicates": 0,
                    },
                )
        self.report.update(scan_id="test", state="running")
        return "test", "history"

    def next_author(self, scan):
        eligible = [(a, p) for a, (s, p) in self.checkpoints.items() if s == "pending"]
        return (
            deepcopy(min(eligible, key=lambda a: a[1]["pages"])) if eligible else None
        )

    def save_author(self, scan, author, checkpoint, state="pending", error=None):
        self.checkpoints[author] = (state, deepcopy(checkpoint))

    def status(self):
        counts = {}
        for state, _ in self.checkpoints.values():
            counts[state] = counts.get(state, 0) + 1
        return {**self.report, "authors": counts}

    def finish(self, scan, state, error=None):
        self.report.update(state=state, error_code=error)
        return self.status()

    def known(self, note):
        return note in self.known_ids

    def queue_count(self):
        return self.queue


class Client:
    def __init__(self, pages, authors=(A, B)):
        self.pages = iter(pages)
        self.authors = authors
        self.calls = []

    def owner(self):
        return "c" * 24

    def following(self, owner):
        return {
            "complete": True,
            "count": len(self.authors),
            "authors": [{"author_id": a, "nickname": "author"} for a in self.authors],
        }

    def author_page(self, author, cursor):
        self.calls.append((author, cursor))
        value = next(self.pages)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def runtime(tmp_path):
    config = SimpleNamespace(
        collection=lambda k: SimpleNamespace(key=k, read_only=False)
    )
    video = SimpleNamespace(root=tmp_path, default_collection="cooking")
    store = Store()
    submitted = []

    def submit(*args, **kwargs):
        duplicate = kwargs["source_note_id"] in store.known_ids
        store.known_ids.add(kwargs["source_note_id"])
        submitted.append(kwargs)
        return {"duplicate": duplicate}

    return config, video, store, submit, submitted


def run(runtime, client, **kwargs):
    config, video, store, submit, _ = runtime
    return sync_following(
        config, video, client=client, store=store, submit=submit, **kwargs
    )


def test_round_robin_and_page_checkpoint_resume(runtime):
    client = Client([page("d" * 24, True, "next"), page("e" * 24)])
    assert run(runtime, client, max_pages=2)["state"] == "partial"
    assert client.calls == [(A, ""), (B, "")]
    continuation = Client([page("f" * 24)])
    assert run(runtime, continuation)["state"] == "complete"
    assert continuation.calls == [(A, "next")]
    assert all(
        x["allow_expired"] is False and x["source_origin"] == "following"
        for x in runtime[4]
    )


def test_verification_stops_all_authors_without_advancing_cursor(runtime):
    client = Client([ProviderUnavailable("XHS_LOGIN_OR_VERIFICATION_REQUIRED")])
    result = run(runtime, client)
    assert result["state"] == "blocked"
    assert len(client.calls) == 1
    assert runtime[2].checkpoints[A][1]["cursor"] == ""
    assert not runtime[4]


def test_cursor_cycle_persists_across_invocations(runtime):
    run(runtime, Client([page("d" * 24, True, "next")], authors=(A,)), max_pages=1)
    result = run(runtime, Client([page("e" * 24, True, "next")], authors=(A,)))
    assert result["state"] == "blocked"
    assert len(runtime[4]) == 1


def test_backpressure_and_pause_submit_nothing(runtime):
    runtime[2].queue = 500
    client = Client([])
    assert run(runtime, client)["state"] == "backpressure"
    assert not client.calls
    runtime[2].queue = 300
    assert run(runtime, Client([]), continue_only=True)["state"] == "backpressure"
    runtime[2].is_paused = True
    assert run(runtime, Client([]))["state"] == "paused"
    assert not runtime[4]


def test_incomplete_snapshot_cannot_start_author_scans(runtime):
    client = Client([])
    client.following = lambda owner: {"complete": False, "authors": []}
    assert run(runtime, client)["error_code"] == "FOLLOWING_ENUMERATION_UNAVAILABLE"
    assert not runtime[2].checkpoints


def test_changed_account_stays_blocked(runtime):
    runtime[2].bound = "d" * 24
    result = run(runtime, Client([]))
    assert result["error_code"] == "XHS_ACCOUNT_CHANGED_RESTART_REQUIRED"


def test_completed_continuation_does_not_touch_platform(runtime):
    runtime[2].report.update(state="complete", scan_id="test")
    client = Client([])
    client.owner = lambda: pytest.fail(
        "Completed continuation must not access platform"
    )
    assert run(runtime, client, continue_only=True)["continuation_skipped"]


def test_head_refresh_preserves_historical_cursor(runtime):
    store = runtime[2]
    store.report.update(state="partial", scan_id="test")
    store.checkpoints[A] = (
        "pending",
        {
            "cursor": "history-next",
            "seen_cursors": [],
            "pages": 1,
            "submitted": 0,
            "duplicates": 0,
            "head_pending": True,
            "head_cursor": "",
            "terminal": False,
        },
    )
    client = Client([page("d" * 24), page("e" * 24)], authors=(A,))
    assert run(runtime, client, continue_only=True)["state"] == "complete"
    assert client.calls == [(A, ""), (A, "history-next")]


@pytest.mark.parametrize(
    "count,users",
    [(2, [{"user_id": A}]), (1, [{"user_id": A}, {"user_id": A}]), (0, [])],
)
def test_exact_following_count_is_required(count, users):
    if count == 2:
        with pytest.raises(ProviderUnavailable, match="COUNT_MISMATCH"):
            normalize_following({"follow_user_d_t_o_list": users}, count)
    else:
        assert (
            len(normalize_following({"follow_user_d_t_o_list": users}, count)) == count
        )


@pytest.mark.parametrize(
    "data,count",
    [
        ({}, 0),
        ({"follow_user_d_t_o_list": []}, None),
        ({"follow_user_d_t_o_list": [{"user_id": "bad"}]}, 1),
    ],
)
def test_missing_schema_never_means_empty_success(data, count):
    with pytest.raises(ProviderUnavailable):
        normalize_following(data, count)
