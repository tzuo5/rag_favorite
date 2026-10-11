from types import SimpleNamespace

import pytest

from rag_favorite import xhs_favorites as favorites
from rag_favorite.video_provider import ProviderUnavailable


class Client:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.cursors = []

    def owner(self):
        return "a" * 24

    def page(self, owner, cursor):
        self.cursors.append(cursor)
        result = next(self.pages)
        if isinstance(result, Exception):
            raise result
        return result


def page(note, more, cursor):
    return {
        "notes": [{"note_id": note, "display_title": "Test", "type": "normal"}],
        "has_more": more,
        "cursor": cursor,
    }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    queued = set()

    def enqueue(
        config, video, url, collection, title, *, source_note_id, allow_expired, source_origin
    ):
        duplicate = source_note_id in queued
        queued.add(source_note_id)
        return {"duplicate": duplicate}

    monkeypatch.setattr(favorites, "enqueue_url", enqueue)
    config = SimpleNamespace(
        collection=lambda key: SimpleNamespace(key=key, read_only=False)
    )
    video = SimpleNamespace(root=tmp_path, default_collection="cooking")
    return config, video, queued


def test_paginated_import_and_repeat_dedup(setup):
    config, video, queued = setup
    pages = [page("b" * 24, True, "next"), page("c" * 24, False, "")]
    result = favorites.sync_favorites(
        config, video, "cooking", client=Client(pages), pause=0
    )
    assert result["state"] == "complete"
    assert result["submitted"] == 2
    assert len(queued) == 2
    repeated = favorites.sync_favorites(
        config, video, "cooking", client=Client(pages), pause=0
    )
    assert repeated["submitted"] == 0
    assert repeated["duplicates"] == 2


def test_resume_after_failure_preserves_cursor(setup):
    config, video, _ = setup
    result = favorites.sync_favorites(
        config,
        video,
        "cooking",
        client=Client(
            [page("b" * 24, True, "next"), ProviderUnavailable("XHS_LOGIN_REQUIRED")]
        ),
        pause=0,
    )
    assert result["state"] == "blocked"
    assert result["cursor"] == "next"
    client = Client([page("c" * 24, False, "")])
    result = favorites.sync_favorites(config, video, "cooking", client=client, pause=0)
    assert client.cursors == ["next"]
    assert result["state"] == "complete"
    assert result["submitted"] == 2


def test_repeated_cursor_cannot_claim_complete(setup):
    config, video, _ = setup
    result = favorites.sync_favorites(
        config,
        video,
        "cooking",
        client=Client([page("b" * 24, True, "next"), page("c" * 24, True, "next")]),
        pause=0,
    )
    assert result["state"] == "blocked"
    assert result["error_code"] == "XHS_FAVORITES_CURSOR_REPEATED"


def test_malformed_page_is_not_empty_success():
    with pytest.raises(ProviderUnavailable):
        favorites.normalize_page({"has_more": False})


def test_signed_tokens_encoded_not_interpreted():
    notes, _, _ = favorites.normalize_page(
        {"notes": [{"note_id": "b" * 24, "xsec_token": "a&b"}], "has_more": False}
    )
    assert "a%26b" in notes[0]["url"]


def test_account_binding_survives_completed_scan_and_restart(setup):
    config, video, queued = setup
    favorites.sync_favorites(
        config, video, "cooking", client=Client([page("b" * 24, False, "")]), pause=0
    )
    another = Client([page("c" * 24, False, "")])
    another.owner = lambda: "d" * 24
    result = favorites.sync_favorites(
        config, video, "cooking", restart=True, client=another, pause=0
    )
    assert result["state"] == "blocked"
    assert result["error_code"] == "XHS_ACCOUNT_CHANGED_RESTART_REQUIRED"
    assert "c" * 24 not in queued


def test_cursor_cycle_survives_resume(setup):
    config, video, _ = setup
    favorites.sync_favorites(
        config,
        video,
        "cooking",
        max_pages=2,
        client=Client([page("b" * 24, True, "one"), page("c" * 24, True, "two")]),
        pause=0,
    )
    result = favorites.sync_favorites(
        config, video, "cooking", client=Client([page("d" * 24, True, "one")]), pause=0
    )
    assert result["state"] == "blocked"
    assert result["error_code"] == "XHS_FAVORITES_CURSOR_REPEATED"


def test_symlink_state_does_not_read_or_overwrite_external_file(setup, tmp_path):
    config, video, _ = setup
    from rag_favorite.config import ConfigError

    path = favorites.state_path(video, "cooking")
    path.parent.mkdir()
    external = tmp_path / "external.json"
    external.write_text('{"secret":"preserve"}')
    path.symlink_to(external)
    with pytest.raises((ConfigError, OSError)):
        favorites.sync_favorites(config, video, "cooking", client=Client([]))
    assert external.read_text() == '{"secret":"preserve"}'


def test_redirect_rejects_external_target_before_network_request():
    import urllib.request

    from rag_favorite.config import ConfigError
    from rag_favorite.xhs_private import TrustedRedirect

    handler = TrustedRedirect(lambda host: host == "edith.xiaohongshu.com")
    req = urllib.request.Request("https://edith.xiaohongshu.com/api")
    for url in [
        "http://edith.xiaohongshu.com/api",
        "https://127.0.0.1/admin",
        "https://edith.xiaohongshu.com:1234/api",
        "https://evil.example/api",
    ]:
        with pytest.raises(ConfigError):
            handler.redirect_request(req, None, 302, "Found", {}, url)


def test_private_state_is_durable_and_owner_only(setup):
    config, video, _ = setup
    favorites.sync_favorites(
        config, video, "cooking", client=Client([page("b" * 24, False, "")]), pause=0
    )
    path = favorites.state_path(video, "cooking")
    assert path.stat().st_mode & 0o077 == 0
    assert path.parent.stat().st_mode & 0o077 == 0
    assert not list(path.parent.glob(".state-*"))


def test_response_validation_rejects_empty_nonterminal_and_oversized_token():
    for response in [
        {"notes": [], "has_more": True, "cursor": "next"},
        {"notes": [{"note_id": "b" * 24, "xsec_token": "x" * 5000}], "has_more": False},
    ]:
        with pytest.raises(ProviderUnavailable):
            favorites.normalize_page(response)


def test_exported_failure_codes_never_contain_private_details(setup):
    config, video, _ = setup
    result = favorites.sync_favorites(
        config,
        video,
        "cooking",
        client=Client([ProviderUnavailable("URL containing private credential")]),
        pause=0,
    )
    assert result["error_code"] == "XHS_FAVORITES_SYNC_FAILED"


def test_browser_lock_prevents_login_and_worker_profile_collision(tmp_path):
    from rag_favorite.xhs_private import browser_session_lock

    def second_browser():
        with browser_session_lock(tmp_path, timeout=0):
            pytest.fail("Second browser should not launch")

    with browser_session_lock(tmp_path):
        error = pytest.raises(ProviderUnavailable, second_browser)
        assert str(error.value) == "XHS_SESSION_BUSY"
    with browser_session_lock(tmp_path, timeout=0):
        pass


@pytest.mark.parametrize(
    "state,expected", [("blocked", 1), ("partial", 1), ("complete", 0)]
)
def test_cli_reports_failed_sync_to_scheduler(setup, monkeypatch, state, expected):
    import sys

    from rag_favorite import video_cli

    config, video, _ = setup
    monkeypatch.setattr(
        sys, "argv", ["rag-video", "import-favorites", "--collection", "cooking"]
    )
    monkeypatch.setattr(video_cli, "load_config", lambda: config)
    monkeypatch.setattr(video_cli, "load_video_config", lambda: video)
    monkeypatch.setattr(
        favorites, "sync_favorites", lambda *args, **kwargs: {"state": state}
    )
    assert video_cli.main() == expected
