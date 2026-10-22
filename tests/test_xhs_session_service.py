import asyncio
import http.cookiejar
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from rag_favorite.video_provider import ProviderUnavailable
from rag_favorite.xhs_session_policy import SessionPolicy, classify, failure
from rag_favorite.xhs_session_rpc import SessionRPC
from rag_favorite.xhs_session_service import SessionService, export_cookies


@pytest.fixture
def policy(tmp_path):
    now = [10000.0]
    return SessionPolicy(tmp_path / "session", clock=lambda: now[0]), now


def test_limits_survive_restart_and_failed_reads_count(policy):
    p, now = policy
    for _ in range(6):
        p.reserve("note")
        p.failed("note", failure("XHS_NETWORK_ERROR"))
        now[0] += 60
    p = SessionPolicy(p.root, clock=lambda: now[0])
    with pytest.raises(ProviderUnavailable) as error:
        p.reserve("note")
    assert error.value.code == "XHS_DEFERRED"
    assert error.value.diagnostics["retry_after"] > 2500
    p.reserve("favorites")  # Shared operation budget, additional note quota.
    with pytest.raises(ProviderUnavailable):
        p.reserve("owner")
    now[0] += 3600
    p.reserve("note")
    assert len(p.data["notes"]) == 7  # The rolling 24-hour budget is retained.


def test_favorites_success_does_not_clear_note_gate(policy):
    p, _ = policy
    p.failed("note", failure("XHS_VERIFICATION_REQUIRED"))
    p.success("favorites")
    p.success("owner")
    assert p.download_gate() == (True, None, False)
    with pytest.raises(ProviderUnavailable, match="VERIFICATION"):
        p.reserve("note")
    p.success("note")
    assert p.download_gate() == (True, None, False)


@pytest.mark.parametrize("status", [401, 403, 461, 471])
def test_http_rejections_are_not_proof_of_logout(status):
    assert classify(status=status).code == "XHS_SESSION_UNKNOWN"
    assert (
        classify(status=status, evidence="verification").code
        == "XHS_VERIFICATION_REQUIRED"
    )


def test_error_evidence_is_specific():
    assert classify(platform=300012).code == "XHS_PLATFORM_BLOCKED"
    assert classify(status=429).code == "XHS_RATE_LIMITED"
    assert classify(status=503).retryable
    assert classify(evidence="login_and_guest").code == "XHS_AUTH_REQUIRED"
    assert classify(status=404).code == "XHS_NOTE_UNAVAILABLE"


def test_cooldown_backoff_and_single_unknown_recheck(policy):
    p, now = policy
    for delay in (1800, 3600, 7200, 7200):
        p.failed("note", failure("XHS_PLATFORM_BLOCKED"))
        assert p.data["paths"]["note"]["retry_at"] == now[0] + delay
        with pytest.raises(ProviderUnavailable):
            p.reserve("note")
        now[0] += delay
        with pytest.raises(ProviderUnavailable):
            p.reserve("note")  # Expired cooldown never releases the risk hold.
        p.reserve("note", interactive=True)
    p.success("note")
    p.failed("note", failure("XHS_SESSION_UNKNOWN"))
    now[0] += 1800
    p.reserve("note", interactive=True)
    p.failed("note", failure("XHS_SESSION_UNKNOWN"))
    assert p.download_gate()[0]
    assert not p.download_gate()[2]


def test_cookie_export_retains_scoping_session_and_expiry(tmp_path):
    cookies = [
        {
            "name": "web_session",
            "value": "PRIVATE_SESSION",
            "domain": ".xiaohongshu.com",
            "path": "/",
            "secure": True,
            "expires": -1,
            "httpOnly": True,
        },
        {
            "name": "web_session",
            "value": "PRIVATE_PATH",
            "domain": "edith.xiaohongshu.com",
            "path": "/api",
            "secure": True,
            "expires": time.time() + 86400,
        },
        {
            "name": "other",
            "value": "PRIVATE_OTHER",
            "domain": ".example.org",
            "path": "/",
            "expires": -1,
        },
    ]
    export_cookies(tmp_path, cookies)
    path = tmp_path / "cookies.txt"
    assert path.stat().st_mode & 0o777 == 0o600
    jar = http.cookiejar.MozillaCookieJar(str(path))
    jar.load(ignore_discard=True)
    assert len(jar) == 2
    values = {c.path: c for c in jar}
    assert values["/"].expires is None and values["/"].discard
    assert values["/api"].value == "PRIVATE_PATH"


def service(tmp_path):
    return SessionService(SimpleNamespace(root=tmp_path / "data"))


def test_single_owner_concurrency_and_no_profile_fallback(tmp_path):
    s = service(tmp_path)

    async def run():
        async with s.lock:
            with pytest.raises(ProviderUnavailable, match="SESSION_BUSY"):
                await s.dispatch("note", {"url": "private"})
            assert (await s.dispatch("status", {}))["service"] == "ready"

    asyncio.run(run())
    with pytest.raises(ProviderUnavailable, match="SESSION_UNAVAILABLE"):
        SessionRPC(s.video).call("owner")
    assert not (s.root / "profile").exists()


def test_wrong_account_does_not_become_authenticated(tmp_path):
    s = service(tmp_path)
    p = s.video.root / "favorites"
    p.mkdir(parents=True)
    binding = p / "owner.json"
    binding.write_text(json.dumps({"owner_hash": "another-account"}))
    binding.chmod(0o600)
    s.signed_get = AsyncMock(return_value={"guest": False, "user_id": "a" * 24})
    with pytest.raises(ProviderUnavailable, match="ACCOUNT_MISMATCH"):
        asyncio.run(s.owner())
    assert s.owner_id is None


def test_fresh_cookies_are_used_for_every_signature(tmp_path):
    s = service(tmp_path)
    values = iter(
        (
            [{"name": "a1", "value": "first", "path": "/"}],
            [{"name": "a1", "value": "second", "path": "/"}],
        )
    )
    signatures = []
    s.ua = "ACTUAL_BROWSER_UA"
    s.sign_session = object()
    s.signer = SimpleNamespace(
        build_url=lambda *_: "https://edith.xiaohongshu.com/api",
        sign_headers_get=lambda uri, cookies, **kw: (
            signatures.append(cookies.copy()) or {}
        ),
    )
    response = SimpleNamespace(
        status=200,
        body=AsyncMock(return_value=b'{"success":true,"data":{}}'),
        dispose=AsyncMock(),
    )
    request = AsyncMock(return_value=response)
    s.context = SimpleNamespace(
        cookies=AsyncMock(side_effect=lambda *_: next(values)),
        request=SimpleNamespace(get=request),
    )

    async def run():
        await s.signed_get("/api", {})
        await s.signed_get("/api", {})

    asyncio.run(run())
    assert [x["a1"] for x in signatures] == ["first", "second"]
    assert request.call_args.kwargs["headers"]["User-Agent"] == s.ua


def test_stale_or_cancelled_phone_operations_rejected(tmp_path):
    s = service(tmp_path)
    s.login = {"id": "attempt", "state": "waiting", "expires_at": time.time() + 600}
    s.frame = {"id": "frame", "at": time.time() - 31}
    with pytest.raises(ProviderUnavailable, match="INTERACTION_EXPIRED"):
        s.check_attempt({"attempt_id": "attempt", "frame_id": "frame"}, frame=True)
    s.login["state"] = "cancelled"
    with pytest.raises(ProviderUnavailable):
        s.check_attempt({"attempt_id": "attempt"})


def test_login_scan_waits_for_owner_and_note_acceptance(tmp_path):
    s = service(tmp_path)
    s.login = {"id": "attempt", "state": "waiting", "expires_at": time.time() + 600}
    locator = SimpleNamespace(
        count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True)
    )
    s.page = SimpleNamespace(locator=lambda *_: SimpleNamespace(first=locator))
    s.evidence = AsyncMock(return_value=None)
    s.read = AsyncMock(return_value="a" * 24)

    def recovered():
        s.policy.data["diagnostics"] = {"pool": {"state": "complete"}}
        return {}

    s.diagnostic_check = AsyncMock(side_effect=[failure("XHS_DEFERRED"), None])

    async def run():
        await s.login_tick()
        assert s.login["state"] == "verifying"
        recovered()
        await s.login_tick()
        assert s.login["state"] == "verifying"
        await s.login_tick()
        assert s.login["state"] == "complete"

    asyncio.run(run())
    assert s.read.call_count == 0  # The bounded diagnostic owns identity checks.


def test_expired_interaction_keeps_profile(tmp_path):
    s = service(tmp_path)
    s.login = {"id": "attempt", "state": "waiting", "expires_at": time.time() - 1}
    profile = s.root / "profile"
    profile.mkdir()
    (profile / "marker").write_text("existing session")
    asyncio.run(s.login_tick())
    assert s.login["state"] == "expired"
    assert (profile / "marker").read_text() == "existing session"


def test_single_absent_note_does_not_gate_the_account(tmp_path):
    s = service(tmp_path)
    s.owner_id = "a" * 24
    s.page = SimpleNamespace(
        url="https://www.xiaohongshu.com/explore/" + "b" * 24,
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        wait_for_timeout=AsyncMock(),
        content=AsyncMock(
            return_value='<script>window.__INITIAL_STATE__={"notes":{}}</script>'
        ),
    )
    s.evidence = AsyncMock(return_value=None)
    s.context = SimpleNamespace(cookies=AsyncMock(return_value=[]))
    s.policy.success("note")
    with pytest.raises(ProviderUnavailable, match="XHS_NOTE_UNAVAILABLE"):
        asyncio.run(s.read("note", {"url": s.page.url}))
    assert s.policy.download_gate() == (False, None, False)
    assert s.policy.data["counters"]["XHS_NOTE_UNAVAILABLE"] == 1


def test_observation_is_local_and_contains_no_cookie_material(tmp_path):
    s = service(tmp_path)
    s.context = SimpleNamespace(
        cookies=lambda: pytest.fail("Observation must not touch browser state")
    )
    s.observe()
    data = json.loads((s.root / "observations.jsonl").read_text().splitlines()[0])
    assert "counters" in data and "at" in data
    assert "cookies" not in data and "profile" not in data
