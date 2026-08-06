from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.ingestion.batch_service import BatchConfirmationService
from backend.ingestion.config import Settings
from backend.ingestion.discovery.models import DiscoveryAdapterError
from backend.ingestion.discovery.registry import capability_payload
from backend.ingestion.discovery_worker import AuthorDiscoveryWorker
from backend.ingestion.models import BatchState, Destination, Platform
from backend.ingestion.service import VideoIngestionService
from backend.xhs_session.browser import BrowserSession, ProfileCorrupt
from backend.xhs_session.cli import SessionManager
from backend.xhs_session.cookie_export import (
    CookieExportError,
    atomic_export_cookies,
    netscape_cookie_bytes,
    validate_cookie_file,
)
from backend.xhs_session.lock import SessionAlreadyRunning, SessionLock
from backend.xhs_session.login_probe import (
    ProbeResult,
    SessionStatus,
    _status_from_payload,
    probe_browser_context,
)
from backend.xhs_session.recovery import (
    probe_session_now,
    trigger_session_refresh,
)
from backend.xhs_session.state import (
    read_status,
    status_is_fresh_and_valid,
    utc_timestamp,
    write_status,
)


def browser_cookies() -> list[dict]:
    return [
        {
            "domain": ".xiaohongshu.com",
            "path": "/",
            "name": "a1",
            "value": "a" * 52,
            "secure": True,
            "expires": 2_000_000_000,
        },
        {
            "domain": ".xiaohongshu.com",
            "path": "/",
            "name": "web_session",
            "value": "session-secret",
            "secure": True,
            "expires": 2_000_000_000,
        },
        {
            "domain": ".example.com",
            "path": "/",
            "name": "unrelated",
            "value": "must-not-be-exported",
        },
        {
            "domain": ".evilxiaohongshu.com",
            "path": "/",
            "name": "attacker",
            "value": "must-not-be-exported",
        },
    ]


def test_generic_code_minus_one_is_not_treated_as_expired_auth() -> None:
    assert (
        _status_from_payload({"success": False, "code": -1})
        == SessionStatus.UNKNOWN
    )


def test_browser_login_signal_recovers_from_unsigned_selfinfo_406() -> None:
    class Response:
        status = 406

        @staticmethod
        def json():
            return {"success": False, "code": -1}

    class Request:
        @staticmethod
        def get(*_args, **_kwargs):
            return Response()

    class Context:
        request = Request()

        @staticmethod
        def cookies():
            return browser_cookies()

    class Locator:
        def __init__(self, visible):
            self.first = self
            self.visible = visible

        def is_visible(self, **_kwargs):
            return self.visible

    class Page:
        @staticmethod
        def locator(selector):
            return Locator("li.user.side-bar-component" in selector)

    assert (
        probe_browser_context(Context(), Page()).status
        == SessionStatus.SESSION_VALID
    )


def test_netscape_export_filters_domains_and_requires_auth() -> None:
    payload = netscape_cookie_bytes(browser_cookies()).decode()
    assert ".xiaohongshu.com\tTRUE\t/\tTRUE" in payload
    assert "unrelated" not in payload
    with pytest.raises(CookieExportError):
        netscape_cookie_bytes(browser_cookies()[:1])


def test_atomic_export_is_private_and_keeps_old_file_on_replace_failure(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "cookies.txt"
    target.write_text("old-cookie-file")
    target.chmod(0o600)
    real_replace = os.replace

    def fail_replace(source, destination):
        if Path(destination) == target:
            raise OSError("injected")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(CookieExportError):
        atomic_export_cookies(target, browser_cookies())
    assert target.read_text() == "old-cookie-file"
    assert not list(tmp_path.glob(".*.tmp"))

    monkeypatch.setattr(os, "replace", real_replace)
    atomic_export_cookies(target, browser_cookies())
    assert target.stat().st_mode & 0o777 == 0o600
    validate_cookie_file(target)


def test_secure_status_file_rejects_extra_fields_and_insecure_mode(
    tmp_path,
) -> None:
    path = tmp_path / "state/session-status.json"
    written = write_status(
        path,
        {
            "file_status": "ready",
            "session_status": "SESSION_VALID",
            "last_probe_at": utc_timestamp(),
            "last_success_at": utc_timestamp(),
            "manual_login_required": False,
            "cookie": "must-not-persist",
        },
    )
    assert "cookie" not in written
    assert "cookie" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert read_status(path)["session_status"] == "SESSION_VALID"
    path.chmod(0o644)
    assert read_status(path)["session_status"] == "UNKNOWN"


def test_capabilities_report_real_session_separately_from_cookie_shape(
    tmp_path,
) -> None:
    settings = session_settings(tmp_path)
    write_status(
        settings.xiaohongshu_status_file,
        {
            "file_status": "ready",
            "session_status": "AUTH_EXPIRED",
            "last_probe_at": utc_timestamp(),
            "manual_login_required": True,
        },
    )
    xhs = capability_payload(settings)["author_batch"]["xiaohongshu"]
    assert xhs["auth_status"] == "AUTH_EXPIRED"
    assert xhs["session"]["session_status"] == "AUTH_EXPIRED"
    assert xhs["session"]["manual_login_required"] is True


def test_session_lock_prevents_concurrent_profile_use(tmp_path) -> None:
    first = SessionLock(tmp_path / "runtime/session.lock")
    second = SessionLock(tmp_path / "runtime/session.lock")
    first.acquire()
    try:
        with pytest.raises(SessionAlreadyRunning):
            second.acquire()
    finally:
        first.release()


def test_corrupt_chromium_local_state_is_detected_before_launch(
    tmp_path, monkeypatch
) -> None:
    settings = session_settings(tmp_path)
    settings.xiaohongshu_profile_dir.mkdir(parents=True)
    (settings.xiaohongshu_profile_dir / "Local State").write_text("{")
    monkeypatch.setenv("DISPLAY", ":99")
    with pytest.raises(ProfileCorrupt):
        BrowserSession(settings).open()


def test_session_refresh_trigger_is_feature_gated(tmp_path) -> None:
    calls = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    disabled = Settings(
        xiaohongshu_session_enabled=False,
        xiaohongshu_session_root=tmp_path,
    )
    assert not trigger_session_refresh(disabled, runner=runner)
    assert calls == []

    enabled = Settings(
        xiaohongshu_session_enabled=True,
        xiaohongshu_session_root=tmp_path,
    )
    assert trigger_session_refresh(enabled, runner=runner)
    command = calls[0][0][0]
    assert command == [
        "systemctl",
        "--user",
        "start",
        "--no-block",
        "xhs-session-manager.service",
    ]

    calls.clear()
    assert probe_session_now(enabled, runner=runner)
    probe_command = calls[0][0][0]
    assert probe_command[-3:] == [
        "backend.xhs_session.cli",
        "status",
        "--probe",
    ]


@pytest.mark.parametrize(
    ("code", "expected_state", "expected_trigger_count"),
    [
        ("AUTH_EXPIRED", BatchState.PAUSED_AUTH, 1),
        ("BOT_CHECK", BatchState.PAUSED_RATE_LIMIT, 0),
        ("RATE_LIMITED", BatchState.PAUSED_RATE_LIMIT, 0),
    ],
)
def test_batch_failure_pauses_by_exact_platform_reason(
    tmp_path,
    monkeypatch,
    code,
    expected_state,
    expected_trigger_count,
) -> None:
    class Repository:
        def __init__(self):
            self.pauses = []

        def record_platform_failure(self, *args, **kwargs):
            return {"circuit_state": "OPEN", "blocked_until": None}

        def pause_and_requeue_batch_child(self, job_id, **kwargs):
            self.pauses.append((job_id, kwargs))

    triggers = []
    monkeypatch.setattr(
        "backend.xhs_session.recovery.trigger_session_refresh",
        lambda settings: triggers.append(settings),
    )
    service = VideoIngestionService.__new__(VideoIngestionService)
    service.settings = Settings(
        xiaohongshu_session_enabled=True,
        xiaohongshu_session_root=tmp_path,
    )
    service.sql = Repository()
    service._handle_batch_failure(
        {
            "id": "job",
            "state": "DOWNLOADING",
            "source_platform": "xiaohongshu",
        },
        DiscoveryAdapterError(code, "safe platform message"),
        "PROCESSING_FAILED",
        True,
    )
    assert service.sql.pauses[0][1]["state"] == expected_state
    assert service.sql.pauses[0][1]["error_code"] == code
    assert len(triggers) == expected_trigger_count


class FakeBrowser:
    def __init__(self, settings, probes, *, login_result=None):
        self.settings = settings
        self.probes = list(probes)
        self.login_result = login_result

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def probe(self):
        return self.probes.pop(0)

    def cookies(self):
        return browser_cookies()

    def capture_login_qr(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")
        path.chmod(0o600)

    def wait_for_login(self, timeout_seconds):
        return self.login_result


class FakeLoginNotifier:
    def __init__(self):
        self.sent = []
        self.succeeded = []

    def send_qr(self, path):
        self.sent.append(path)
        return [{"chat_id": "1", "message_id": 2}]

    def login_succeeded(self, messages):
        self.succeeded.append(messages)


class FakeRecoveryRepository:
    def resume_recovered_xiaohongshu_batches(self):
        return 3

    def resume_recovered_xiaohongshu_jobs(self):
        return 2

    def resume_recovered_xiaohongshu_discoveries(self):
        return 1


def session_settings(tmp_path) -> Settings:
    return Settings(
        xiaohongshu_session_enabled=True,
        xiaohongshu_session_root=tmp_path / "session",
        xiaohongshu_cookies_file=str(tmp_path / "secrets/cookies.txt"),
    )


def test_refresh_exports_only_after_two_real_success_probes(tmp_path) -> None:
    settings = session_settings(tmp_path)

    def factory(_settings):
        return FakeBrowser(
            _settings,
            [
                ProbeResult(SessionStatus.SESSION_VALID),
                ProbeResult(SessionStatus.SESSION_VALID),
                ProbeResult(SessionStatus.SESSION_VALID),
            ],
        )

    manager = SessionManager(
        settings,
        browser_factory=factory,
        notifier=FakeLoginNotifier(),
        repository_factory=lambda _: FakeRecoveryRepository(),
    )
    result = manager.refresh()
    assert result["session_status"] == "SESSION_VALID"
    assert result["resumed_batch_count"] == 3
    assert result["resumed_job_count"] == 2
    assert result["resumed_discovery_count"] == 1
    validate_cookie_file(Path(settings.xiaohongshu_cookies_file))


def test_xiaohongshu_auth_discovery_requeues_and_triggers_login(
    tmp_path,
    monkeypatch,
) -> None:
    class Repository:
        settings = Settings(
            xiaohongshu_session_enabled=True,
            xiaohongshu_session_root=tmp_path,
        )

        def __init__(self):
            self.failed = []
            self.requeued = []

        @staticmethod
        def claim_author_discovery():
            return {
                "id": "discovery",
                "platform": "xiaohongshu",
                "canonical_author_url": (
                    "https://www.xiaohongshu.com/user/profile/test"
                ),
                "scan_limit": 1,
            }

        @staticmethod
        def acquire_platform_request(_platform, probe=False):
            return SimpleNamespace(allowed=True)

        @staticmethod
        def record_platform_failure(*_args, **_kwargs):
            return None

        def requeue_author_discovery(self, discovery_id, reason):
            self.requeued.append((discovery_id, reason))
            return {"id": discovery_id, "state": "QUEUED"}

        def fail_author_discovery(self, *args):
            self.failed.append(args)
            return {"state": "FAILED"}

    class Adapter:
        platform = Platform.XIAOHONGSHU

        @staticmethod
        async def discover(*_args, **_kwargs):
            raise DiscoveryAdapterError(
                "AUTH_EXPIRED", "authentication expired"
            )

    triggers = []
    monkeypatch.setattr(
        "backend.xhs_session.recovery.trigger_session_refresh",
        lambda settings: triggers.append(settings) or True,
    )
    repository = Repository()
    result = asyncio.run(
        AuthorDiscoveryWorker(repository, Adapter()).run_once()
    )
    assert result["state"] == "QUEUED"
    assert repository.requeued == [("discovery", "AUTH_EXPIRED")]
    assert repository.failed == []
    assert triggers == [repository.settings]


def test_login_qr_is_deleted_after_success(tmp_path) -> None:
    settings = session_settings(tmp_path)
    notifier = FakeLoginNotifier()

    def factory(_settings):
        return FakeBrowser(
            _settings,
            [
                ProbeResult(SessionStatus.AUTH_EXPIRED),
                ProbeResult(SessionStatus.SESSION_VALID),
                ProbeResult(SessionStatus.SESSION_VALID),
            ],
            login_result=ProbeResult(SessionStatus.SESSION_VALID),
        )

    result = SessionManager(
        settings,
        browser_factory=factory,
        notifier=notifier,
        repository_factory=lambda _: FakeRecoveryRepository(),
    ).login()
    assert result["session_status"] == "SESSION_VALID"
    assert len(notifier.sent) == 1
    assert len(notifier.succeeded) == 1
    assert not settings.xiaohongshu_qr_file.exists()


def test_fresh_session_status_is_required_for_enabled_xhs_confirmation(
    tmp_path,
) -> None:
    settings = Settings(
        author_batch_xiaohongshu_enabled=True,
        xiaohongshu_session_enabled=True,
        xiaohongshu_session_root=tmp_path,
    )

    class Repository:
        def __init__(self):
            self.settings = settings

        def get_author_discovery(self, discovery_id, user_id):
            return {"id": discovery_id, "platform": "xiaohongshu"}

        def platform_gate(self, platform):
            return None

        def confirm_discovery(self, *args, **kwargs):
            return {"id": "batch", "state": "QUEUED"}

    status = {
        "session_status": "SESSION_VALID",
        "last_probe_at": utc_timestamp(),
    }
    service = BatchConfirmationService(
        Repository(),
        disk_usage=lambda _: SimpleNamespace(free=20 * 2**30),
        session_status_reader=lambda _: status,
    )
    assert (
        service.confirm("discovery", "user", Destination.GENERAL)["state"] == "QUEUED"
    )
    assert status_is_fresh_and_valid(
        status,
        max_age_seconds=900,
        now=datetime.now(timezone.utc),
    )


def test_stale_confirmation_probes_and_continues_when_session_recovers(
    tmp_path,
) -> None:
    settings = Settings(
        author_batch_xiaohongshu_enabled=True,
        xiaohongshu_session_enabled=True,
        xiaohongshu_session_root=tmp_path,
    )
    status = {
        "session_status": "UNKNOWN",
        "last_probe_at": None,
    }

    class Repository:
        def __init__(self):
            self.settings = settings

        def get_author_discovery(self, discovery_id, user_id):
            return {"id": discovery_id, "platform": "xiaohongshu"}

        def platform_gate(self, platform):
            return None

        def confirm_discovery(self, *args, **kwargs):
            return {"id": "batch", "state": "QUEUED"}

    def probe(_settings):
        status.update(
            {
                "session_status": "SESSION_VALID",
                "last_probe_at": utc_timestamp(),
            }
        )
        return True

    service = BatchConfirmationService(
        Repository(),
        disk_usage=lambda _: SimpleNamespace(free=20 * 2**30),
        session_status_reader=lambda _: status,
        session_probe=probe,
    )
    assert (
        service.confirm("discovery", "user", Destination.GENERAL)["state"] == "QUEUED"
    )
