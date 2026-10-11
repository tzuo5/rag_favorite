import copy
import json
import threading
import time
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from rag_favorite import web_console as web

ORIGIN = "https://console.example.ts.net"
PASSWORD = "test-password-at-least-16"
HEADERS = {
    "x-forwarded-proto": "https",
    "x-forwarded-for": "100.64.0.2",
    "tailscale-user-login": "owner@example.com",
    "origin": ORIGIN,
}
SNAPSHOT = {
    "captured_at": "2026-10-06T00:00:00+00:00",
    "counts": {"queued": 1},
    "total": 1,
    "paused_pending": 0,
    "control_supported": True,
    "truncated": False,
    "jobs": [
        {
            "id": str(uuid4()),
            "title": "<script>alert(1)</script>",
            "state": "queued",
            "payload": {"token": "PRIVATE_PAYLOAD"},
        }
    ],
    "models": [{"key": "CCR", "name": "model", "api_key": "PRIVATE_KEY"}],
    "control": {"paused": False, "paused_jobs": [], "private": "PRIVATE_CONTROL"},
    "worker": {"ActiveState": "active", "MainPID": "123"},
    "logs": ["safe log"],
    "secret": "PRIVATE_CONFIG",
}


class Backend:
    def __init__(self):
        self.fail = False
        self.calls = []
        self.entered = threading.Event()
        self.release = None

    def snapshot(self):
        if self.fail:
            raise RuntimeError("PRIVATE_DATABASE_PASSWORD")
        return copy.deepcopy(SNAPSHOT)

    def action(self, name, job_id=None):
        self.calls.append((name, job_id))
        self.entered.set()
        if self.release:
            self.release.wait(5)
        if self.fail:
            raise RuntimeError("PRIVATE_DATABASE_PASSWORD")
        return "操作已完成"


@pytest.fixture
def setup(tmp_path):
    settings = {
        "origin": ORIGIN,
        "owner_login": "owner@example.com",
        "username": "admin",
        "allowed_ips": ["100.64.0.1", "100.64.0.2"],
        "self_ips": ["100.64.0.1"],
        "salt": "aa" * 16,
        "password_hash": web.password_hash(PASSWORD, "aa" * 16),
    }
    path = tmp_path / "settings.json"
    web.write_private(path, json.dumps(settings))
    backend = Backend()
    app = web.create_app(backend, web.load_settings(path))
    with TestClient(
        app, base_url=ORIGIN, client=("127.0.0.1", 1234), headers=HEADERS
    ) as client:
        yield client, backend, app, path


def login(client):
    response = client.post(
        "/api/login", json={"username": "admin", "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf"]


@pytest.mark.parametrize(
    "headers",
    [
        {"x-forwarded-for": "100.64.0.3"},
        {"tailscale-user-login": "other@example.com"},
        {"x-forwarded-proto": "http"},
        {"host": "evil.example"},
        {"tailscale-funnel-request": "?1"},
        {"x-forwarded-for": "100.64.0.2, 100.64.0.1"},
        {"x-forwarded-for": "garbage"},
        {"tailscale-user-login": ""},
    ],
)
def test_network_boundary_denies_every_route(setup, headers):
    client, _, _, _ = setup
    for path in ["/", "/assets/console.js", "/api/session", "/api/snapshot"]:
        assert client.get(path, headers=headers).status_code == 403


def test_non_loopback_cannot_spoof_serve_headers(setup):
    _, _, app, _ = setup
    with TestClient(
        app, base_url=ORIGIN, client=("192.168.1.2", 1234), headers=HEADERS
    ) as client:
        assert client.get("/").status_code == 403


def test_login_session_csrf_and_logout(setup):
    client, backend, _, _ = setup
    assert client.get("/api/snapshot").status_code == 401
    token = login(client)
    assert client.get("/api/session").json()["csrf"] == token
    request = {"action": "stop", "request_id": str(uuid4())}
    assert client.post("/api/action", json=request).status_code == 403
    assert (
        client.post(
            "/api/action",
            json=request,
            headers={"x-csrf-token": token, "origin": "https://evil.invalid"},
        ).status_code
        == 403
    )
    assert not backend.calls
    assert (
        client.post(
            "/api/action", json=request, headers={"x-csrf-token": token}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/action", json=request, headers={"x-csrf-token": token}
        ).status_code
        == 409
    )
    assert backend.calls == [("stop", None)]
    assert (
        client.post("/api/logout", json={}, headers={"x-csrf-token": token}).status_code
        == 200
    )
    assert client.get("/api/snapshot").status_code == 401


def test_cookies_security_and_device_binding(setup):
    client, _, _, _ = setup
    response = client.post(
        "/api/login", json={"username": "admin", "password": PASSWORD}
    )
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Domain=" not in cookie and "Path=/" in cookie
    assert (
        client.get(
            "/api/session", headers={"x-forwarded-for": "100.64.0.1"}
        ).status_code
        == 401
    )
    assert client.get("/").headers["cache-control"] == "no-store"
    assert (
        "frame-ancestors 'none'" in client.get("/").headers["content-security-policy"]
    )


def test_login_origin_limits_and_non_ascii_input(setup):
    client, _, _, _ = setup
    assert (
        client.post(
            "/api/login",
            json={"username": "admin", "password": PASSWORD},
            headers={"origin": "https://evil.invalid"},
        ).status_code
        == 403
    )
    for _ in range(5):
        assert (
            client.post(
                "/api/login", json={"username": "非法用户名", "password": "wrong"}
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/api/login", json={"username": "admin", "password": PASSWORD}
        ).status_code
        == 429
    )


def test_snapshot_redaction_stale_and_recovery(setup):
    client, backend, app, _ = setup
    login(client)
    client.portal.call(app.state.refresh)
    response = client.get("/api/snapshot")
    assert response.status_code == 200 and not response.json()["stale"]
    assert "PRIVATE" not in response.text
    backend.fail = True
    client.portal.call(app.state.refresh)
    response = client.get("/api/snapshot")
    assert response.status_code == 503 and response.json()["snapshot"]["total"] == 1
    assert "PRIVATE" not in response.text
    backend.fail = False
    client.portal.call(app.state.refresh)
    assert client.get("/api/snapshot").status_code == 200


def test_actions_are_strict_and_errors_do_not_leak(setup):
    client, backend, _, _ = setup
    token = login(client)
    headers = {"x-csrf-token": token}
    for action in [
        {"action": "delete", "request_id": str(uuid4())},
        {"action": "stop_job", "job_id": "INVALID", "request_id": str(uuid4())},
        {"action": "stop", "request_id": str(uuid4()), "secret": "PRIVATE"},
    ]:
        response = client.post("/api/action", json=action, headers=headers)
        assert response.status_code == 422 and "PRIVATE" not in response.text
    assert (
        client.post(
            "/api/action",
            json={"action": "stop_job", "request_id": str(uuid4())},
            headers=headers,
        ).status_code
        == 400
    )
    assert not backend.calls
    backend.fail = True
    response = client.post(
        "/api/action",
        json={"action": "stop", "request_id": str(uuid4())},
        headers=headers,
    )
    assert response.status_code == 409 and "PRIVATE" not in response.text


def test_parallel_actions_are_rejected(setup):
    client, backend, _, _ = setup
    token = login(client)
    backend.release = threading.Event()
    result = []

    def first():
        result.append(
            client.post(
                "/api/action",
                json={"action": "stop", "request_id": str(uuid4())},
                headers={"x-csrf-token": token},
            )
        )

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert backend.entered.wait(3)
        response = client.post(
            "/api/action",
            json={"action": "start", "request_id": str(uuid4())},
            headers={"x-csrf-token": token},
        )
        assert response.status_code == 409
    finally:
        backend.release.set()
        thread.join(5)
    assert result[0].status_code == 200 and len(backend.calls) == 1


def test_expired_sessions_fail_closed(setup, monkeypatch):
    client, _, _, _ = setup
    monkeypatch.setattr(web, "SESSION_SECONDS", 0.01)
    login(client)
    time.sleep(0.02)
    assert client.get("/api/session").status_code == 401


def test_local_self_and_request_size(setup):
    client, _, _, _ = setup
    assert (
        client.get(
            "/", headers={"x-forwarded-for": "100.64.0.1", "tailscale-user-login": ""}
        ).status_code
        == 200
    )
    response = client.post("/api/login", content="a" * 5000)
    assert response.status_code == 413
    response = client.post(
        "/api/login", json={"username": "admin", "password": "PRIVATE" * 45}
    )
    assert response.status_code == 422 and "PRIVATE" not in response.text


def test_settings_require_private_permissions(setup):
    _, _, _, path = setup
    path.chmod(0o644)
    with pytest.raises(ValueError, match="600"):
        web.load_settings(path)
