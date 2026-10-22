"""Account pacing and recovery must stay independent of proxy rotation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_xhs_proxy import pool

from rag_favorite.video_provider import ProviderUnavailable
from rag_favorite.xhs_session_policy import SessionPolicy, failure
from rag_favorite.xhs_session_service import SessionService


def test_shared_hourly_budget_and_failures_survive_restart(tmp_path):
    now = [10000.0]
    policy = SessionPolicy(tmp_path, clock=lambda: now[0])
    for index in range(30):
        operation = ["owner", "favorites", "following", "author", "login"][index % 5]
        policy.reserve(operation, interactive=True)
        policy.failed(operation, failure("XHS_NETWORK_ERROR"))
        now[0] += 60
    policy = SessionPolicy(tmp_path, clock=lambda: now[0])
    assert policy.budget()["operations_remaining"] == 0
    with pytest.raises(ProviderUnavailable, match="DEFERRED"):
        policy.reserve("owner", interactive=True)
    now[0] += 1800
    policy.reserve("owner", interactive=True)


@pytest.mark.parametrize(
    "code",
    [
        "XHS_AUTH_REQUIRED",
        "XHS_VERIFICATION_REQUIRED",
        "XHS_RATE_LIMITED",
        "XHS_PLATFORM_BLOCKED",
        "XHS_SESSION_UNKNOWN",
        "XHS_ACCOUNT_MISMATCH",
    ],
)
def test_risk_stays_account_wide_after_success_restart_and_cooldown(tmp_path, code):
    now = [10000.0]
    p = SessionPolicy(tmp_path, clock=lambda: now[0])
    p.failed("note", failure(code))
    p.success("note")
    now[0] += 86400
    p = SessionPolicy(tmp_path, clock=lambda: now[0])
    for operation in (
        "owner",
        "favorites",
        "following",
        "author",
        "note",
        "media",
        "login",
    ):
        with pytest.raises(ProviderUnavailable):
            p.reserve(operation)
    assert p.download_gate()[0]


def test_direct_diagnostic_never_changes_pool_progress_or_admits_production(tmp_path):
    from copy import deepcopy

    p = pool(tmp_path)
    for i in range(80):
        p.complete(str(i), p.data["generation"])
    before = deepcopy(p.data)
    p.set_route("direct_diagnostic")
    assert p.outbound() == {"protocol": "freedom", "settings": {}}
    p.before_request(diagnostic=True)
    assert p.data == before
    with pytest.raises(ProviderUnavailable, match="ACCEPTANCE_PAUSED"):
        p.before_request()
    p.set_route("pool")
    assert p.outbound()["protocol"] == "vless"
    assert p.data == before


def test_two_hour_observation_requires_six_extra_distinct_publications(tmp_path):
    from rag_favorite.xhs_acceptance import advance

    now = [10000.0]
    p = SessionPolicy(tmp_path, clock=lambda: now[0])
    p.data["recovery"].update(phase="acceptance", accepted={})
    evidence = lambda n: {str(i): {"note_id": str(i)} for i in range(n)}
    assert not advance(p, evidence(2))
    assert p.download_gate()[0]
    assert advance(p, evidence(3))
    assert p.budget()["notes_per_hour"] == 6
    assert not advance(p, evidence(9))
    now[0] += 7199
    assert not advance(p, {})
    now[0] += 1
    assert advance(p, {})
    assert p.budget()["notes_per_hour"] == 12
    p.failed("owner", failure("XHS_SESSION_UNKNOWN"))
    assert not advance(p, evidence(20))
    assert p.download_gate()[0]


def test_cached_sources_and_media_retries_obey_note_quota(tmp_path):
    p = SessionPolicy(tmp_path, clock=lambda: 10000)
    for i in range(6):
        p.reserve_media_note(str(i), 1)
    with pytest.raises(ProviderUnavailable, match="DEFERRED"):
        p.reserve_media_note("seventh", 1)
    p = SessionPolicy(tmp_path, clock=lambda: 10001)
    assert p.budget()["notes_used"] == 6
    assert p.budget()["operations_used"] == 0


def test_diagnostic_failure_is_bounded_preserves_hold_and_ledger(tmp_path):
    proxy = pool(tmp_path)
    s = SessionService(
        SimpleNamespace(root=tmp_path / "data", xhs_proxy_config=proxy.config_path)
    )
    s.policy.clock = lambda: 10000
    s.proxy.ensure_started = lambda **kw: False
    s.page = SimpleNamespace(
        url="about:blank",
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        screenshot=AsyncMock(),
    )
    s.evidence = AsyncMock(return_value="platform_risk")
    s.export = AsyncMock()
    generation = s.proxy.data["generation"]

    async def run():
        with pytest.raises(ProviderUnavailable, match="PLATFORM_BLOCKED"):
            await s.dispatch("diagnose", {"route": "direct_diagnostic"})
        first = s.policy.data["counters"]["page_attempts"]
        await s.dispatch("diagnose", {"route": "direct_diagnostic"})
        assert s.policy.data["counters"]["page_attempts"] == first
        assert (
            s.policy.data["diagnostics"]["direct_diagnostic"]["platform_code"]
            == "300012"
        )
        with pytest.raises(ProviderUnavailable, match="ACCEPTANCE_PAUSED"):
            await s.dispatch("network_resume", {})

    asyncio.run(run())
    assert s.proxy.data["generation"] == generation
    assert s.proxy.data["total_completed"] == 0
    assert s.proxy.route_mode == "pool" and s.policy.download_gate()[0]


def test_proxy_keeps_account_pacing(tmp_path):
    proxy = pool(tmp_path)
    service = SessionService(
        SimpleNamespace(root=tmp_path / "data", xhs_proxy_config=proxy.config_path)
    )
    service.policy.reserve("owner", interactive=True)
    with pytest.raises(ProviderUnavailable, match="XHS_DEFERRED"):
        service.policy.reserve("favorites", interactive=True)
    assert service.status()["limits"]["interval_seconds"] >= 60


def test_single_success_never_releases_recovery_hold(tmp_path):
    policy = SessionPolicy(tmp_path)
    policy.failed("note", failure("XHS_VERIFICATION_REQUIRED"))
    policy.success("note")
    assert policy.download_gate()[0]


def test_platform_risk_does_not_rotate_node(tmp_path):
    proxy = pool(tmp_path)
    service = SessionService(
        SimpleNamespace(root=tmp_path / "data", xhs_proxy_config=proxy.config_path)
    )
    generation = service.proxy.data["generation"]
    service.network_failure("note", failure("XHS_PLATFORM_BLOCKED", platform="300012"))
    assert service.proxy.data["generation"] == generation
    assert service.policy.download_gate()[0]


def test_note_read_does_not_bulk_requeue(tmp_path):
    mirror = SimpleNamespace(recovered=Mock(), publish=Mock())
    service = SessionService(SimpleNamespace(root=tmp_path / "data"), mirror=mirror)
    service.owner_id = "a" * 24
    service.ensure_network = AsyncMock()
    service.export = AsyncMock()
    service.note = AsyncMock(return_value={"note_id": "b" * 24})
    asyncio.run(service.read("note", {"url": "private"}, interactive=True))
    mirror.recovered.assert_not_called()


def test_successful_diagnostic_checks_owner_then_note_without_unlock(tmp_path):
    proxy = pool(tmp_path)
    now = [10000.0]
    s = SessionService(
        SimpleNamespace(root=tmp_path / "data", xhs_proxy_config=proxy.config_path)
    )
    s.policy.clock = lambda: now[0]
    s.proxy.ensure_started = lambda **kw: False
    s.mirror = SimpleNamespace(
        publish=Mock(),
        candidate=Mock(return_value="https://www.xiaohongshu.com/explore/" + "b" * 24),
        recovered=Mock(),
    )
    s.page = SimpleNamespace(
        url="about:blank",
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        screenshot=AsyncMock(),
    )
    s.evidence = AsyncMock(return_value=None)
    s.export = AsyncMock()

    async def owner():
        s.owner_id = "a" * 24
        return s.owner_id

    s.owner = AsyncMock(side_effect=owner)
    s.note = AsyncMock(return_value={"note_id": "b" * 24})

    async def run():
        for _ in range(3):
            await s.dispatch("diagnose", {"route": "pool"})
            now[0] += 60
        assert s.policy.data["diagnostics"]["pool"]["state"] == "complete"
        assert s.policy.data["recovery"]["phase"] == "validated"
        assert s.policy.download_gate()[0]
        await s.dispatch("diagnose", {"route": "pool"})

    asyncio.run(run())
    s.owner.assert_awaited_once()
    s.note.assert_awaited_once()
    s.mirror.recovered.assert_not_called()
    assert s.policy.budget()["operations_used"] == 3
    assert s.proxy.data["total_completed"] == 0


def test_rolling_day_note_budget_survives_restart(tmp_path):
    now = [10000.0]
    p = SessionPolicy(tmp_path, clock=lambda: now[0])
    p.data["recovery"]["phase"] = "steady"
    for _ in range(100):
        p.reserve("note")
        now[0] += 601
    p = SessionPolicy(tmp_path, clock=lambda: now[0])
    with pytest.raises(ProviderUnavailable, match="DEFERRED"):
        p.reserve("note")
    assert p.budget()["notes_used_24h"] == 100
    now[0] = 10000 + 86400
    p.reserve("note")


def test_cancellation_and_route_change_wait_for_active_media(tmp_path, monkeypatch):
    import threading

    from rag_favorite import xhs_media_service

    proxy = pool(tmp_path)
    started, finish = threading.Event(), threading.Event()
    s = SessionService(
        SimpleNamespace(root=tmp_path / "data", xhs_proxy_config=proxy.config_path)
    )
    s.policy.data["recovery"]["phase"] = "initial"
    s.proxy.ensure_started = lambda: False
    s.proxy.data["node_validated_generation"] = s.proxy.data["generation"]
    s.read = AsyncMock()

    class Bundle:
        def __init__(self, *args):
            self.data = {
                "note_id": "a" * 24,
                "downloaded": True,
                "complete": True,
                "generation": 1,
            }

        def ready(self):
            return False

        def download(self, *args):
            started.set()
            assert finish.wait(5)
            return self.data

    monkeypatch.setattr(xhs_media_service, "MediaBundle", Bundle)

    async def run():
        task = asyncio.create_task(
            s.dispatch(
                "download_media", {"job_id": "fixture", "note": {"note_id": "a" * 24}}
            )
        )
        await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        with pytest.raises(ProviderUnavailable, match="SESSION_BUSY"):
            await s.dispatch("diagnose", {"route": "direct_diagnostic"})
        assert s.proxy.route_mode == "pool" and s.proxy.data["generation"] == 1
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert s.proxy.data["total_completed"] == 1


def test_login_risk_stops_without_qr_or_node_rotation(tmp_path):
    proxy = pool(tmp_path)
    s = SessionService(
        SimpleNamespace(root=tmp_path / "data", xhs_proxy_config=proxy.config_path)
    )
    s.login = {"state": "waiting"}
    s.page = SimpleNamespace(goto=AsyncMock(), screenshot=AsyncMock())
    s.evidence = AsyncMock(return_value="platform_risk")
    assert asyncio.run(s.stop_risky_login())
    assert s.login["state"] == "blocked"
    assert s.policy.data["paths"]["login"]["platform_code"] == "300012"
    assert s.proxy.data["generation"] == 1
    assert s.policy.download_gate()[0]


def test_cached_source_revalidates_exact_url_on_new_node(tmp_path, monkeypatch):
    from rag_favorite import xhs_media_service

    proxy = pool(tmp_path)
    s = SessionService(
        SimpleNamespace(root=tmp_path / "data", xhs_proxy_config=proxy.config_path)
    )
    s.policy.data["recovery"]["phase"] = "initial"
    s.ensure_network = AsyncMock()
    s.read = AsyncMock(side_effect=["owner", failure("XHS_DEFERRED")])
    source = "https://www.xiaohongshu.com/explore/" + "a" * 24 + "?xsec_token=fixture"
    s.mirror = SimpleNamespace(source_for_job=Mock(return_value=source))
    monkeypatch.setattr(
        xhs_media_service,
        "MediaBundle",
        lambda *args: SimpleNamespace(ready=lambda: False),
    )
    with pytest.raises(ProviderUnavailable, match="DEFERRED"):
        asyncio.run(
            s.download_media({"job_id": "fixture", "note": {"note_id": "a" * 24}})
        )
    assert s.read.await_args_list[1].args == ("note", {"url": source})
    assert s.proxy.data["total_completed"] == 0
