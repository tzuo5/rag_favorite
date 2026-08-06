from __future__ import annotations

import http.cookiejar
import stat
from pathlib import Path

from backend.bilibili_session.cookie_export import (
    CookieExportError,
    atomic_export_cookies,
    validate_cookie_file,
)
from backend.bilibili_session.cli import SessionManager
from backend.bilibili_session.recovery import (
    classify_session_error,
    trigger_session_refresh,
)
from backend.ingestion.config import Settings
from backend.ingestion.models import PlatformErrorCode


def cookie(name: str, value: str = "secret") -> http.cookiejar.Cookie:
    return http.cookiejar.Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=".bilibili.com",
        domain_specified=True,
        domain_initial_dot=True,
        path="/",
        path_specified=True,
        secure=True,
        expires=None,
        discard=False,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )


def test_bilibili_cookie_export_is_private_and_complete(
    tmp_path: Path,
) -> None:
    target = tmp_path / "secrets/bilibili.txt"
    atomic_export_cookies(
        target,
        [
            cookie("SESSDATA"),
            cookie("bili_jct"),
            cookie("DedeUserID", "123"),
        ],
    )

    validate_cookie_file(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert "SESSDATA" in target.read_text()


def test_bilibili_cookie_export_rejects_incomplete_session(
    tmp_path: Path,
) -> None:
    target = tmp_path / "cookies.txt"
    try:
        atomic_export_cookies(target, [cookie("SESSDATA")])
    except CookieExportError:
        pass
    else:
        raise AssertionError("incomplete Bilibili cookies were accepted")
    assert not target.exists()


def test_bilibili_refresh_is_feature_gated(tmp_path: Path) -> None:
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return type("Result", (), {"returncode": 0})()

    assert not trigger_session_refresh(
        Settings(
            bilibili_session_enabled=False,
            bilibili_session_root=tmp_path,
        ),
        runner=runner,
    )
    assert not calls
    assert trigger_session_refresh(
        Settings(
            bilibili_session_enabled=True,
            bilibili_session_root=tmp_path,
        ),
        runner=runner,
    )
    assert calls[0][0][-1] == "bilibili-session-manager.service"


def test_bilibili_refresh_uses_launchd_on_macos(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("backend.bilibili_session.recovery.sys.platform", "darwin")
    assert trigger_session_refresh(
        Settings(
            bilibili_session_enabled=True,
            bilibili_session_root=tmp_path,
        ),
        runner=runner,
    )
    assert calls[0][0][:3] == ["launchctl", "kickstart", "-k"]
    assert calls[0][0][-1].endswith("com.rag-favorite.bilibili-session-manager")


def test_bilibili_412_requires_expired_real_session_before_login(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = Settings(
        bilibili_session_enabled=True,
        bilibili_session_root=tmp_path,
    )
    monkeypatch.setattr(
        SessionManager,
        "status",
        lambda self: {"session_status": "AUTH_EXPIRED"},
    )
    expired = classify_session_error(
        settings,
        RuntimeError("HTTP Error 412: Precondition Failed"),
    )
    assert expired.code == PlatformErrorCode.AUTH_EXPIRED

    monkeypatch.setattr(
        SessionManager,
        "status",
        lambda self: {"session_status": "SESSION_VALID"},
    )
    blocked = classify_session_error(
        settings,
        RuntimeError("HTTP Error 412: Precondition Failed"),
    )
    assert blocked.code == PlatformErrorCode.PLATFORM_BLOCKED


def test_verified_bilibili_session_resumes_all_paused_work(
    tmp_path: Path,
) -> None:
    class Repository:
        @staticmethod
        def resume_recovered_bilibili_batches():
            return 3

        @staticmethod
        def resume_recovered_bilibili_jobs():
            return 2

        @staticmethod
        def resume_recovered_bilibili_discoveries():
            return 1

    manager = SessionManager(
        Settings(
            bilibili_session_enabled=True,
            bilibili_session_root=tmp_path,
            bilibili_auto_resume=True,
        ),
        repository_factory=lambda _: Repository(),
    )
    assert manager._resume_work() == {
        "resumed_batch_count": 3,
        "resumed_job_count": 2,
        "resumed_discovery_count": 1,
    }
