import pytest

from rag_favorite.video_provider import ProviderUnavailable
from rag_favorite.video_sources import check_note_redirect, source_http_error


def test_symbolic_provider_errors_survive_without_leaking_messages():
    assert ProviderUnavailable("XHS_LOGIN_REQUIRED").code == "XHS_LOGIN_REQUIRED"
    assert ProviderUnavailable("https://private/?token=secret").code is None


@pytest.mark.parametrize(
    "path,code",
    [
        ("/login?redirectPath=private", "XHS_LOGIN_REQUIRED"),
        ("/404?error_code=300031&error_msg=private", "XHS_NOTE_UNAVAILABLE"),
        (
            "/website-login/error?error_code=300012",
            "XHS_LOGIN_OR_VERIFICATION_REQUIRED",
        ),
    ],
)
def test_redirect_classification_is_specific_and_redacted(path, code):
    with pytest.raises(ProviderUnavailable) as caught:
        check_note_redirect("https://www.xiaohongshu.com" + path)
    assert caught.value.code == code
    assert "private" not in str(caught.value)
    assert not caught.value.retryable


@pytest.mark.parametrize(
    "status,retryable,code",
    [
        (401, False, "XHS_LOGIN_REQUIRED"),
        (403, False, "XHS_LOGIN_OR_VERIFICATION_REQUIRED"),
        (461, False, "XHS_LOGIN_OR_VERIFICATION_REQUIRED"),
        (404, False, "XHS_NOTE_UNAVAILABLE"),
        (429, True, "XHS_SOURCE_HTTP_429"),
        (503, True, "XHS_SOURCE_HTTP_503"),
    ],
)
def test_http_classification(status, retryable, code):
    error = source_http_error(status)
    assert error.code == code and error.retryable is retryable


def test_note_uses_service_without_independent_http(tmp_path, monkeypatch):
    from rag_favorite import video_sources
    from rag_favorite.video_config import VideoConfig
    from rag_favorite.xhs_session_rpc import SessionRPC

    calls = []
    monkeypatch.setattr(SessionRPC, "call", lambda self, operation, **params: calls.append((operation, params)) or {"note_id": "a" * 24})
    monkeypatch.setattr(video_sources.urllib.request, "build_opener", lambda *_: pytest.fail("Duplicate HTTP request"))
    video = VideoConfig(root=tmp_path / "data", credentials_file=tmp_path / "unused")
    url = "https://www.xiaohongshu.com/explore/" + "a" * 24
    assert video_sources.fetch_note(video, url)["note_id"] == "a" * 24
    assert calls == [("note", {"url": url})]
