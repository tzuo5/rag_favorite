import asyncio
import json
import urllib.request
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from rag_favorite.video_config import VideoConfig
from rag_favorite.video_provider import ProviderUnavailable
from rag_favorite.video_store import digest_file
from rag_favorite.xhs_private import atomic_private_json
from rag_favorite.xhs_proxy import ProxyPool, RequiredProxy
from rag_favorite.xhs_session_policy import SessionPolicy, failure
from rag_favorite.xhs_session_service import SessionService


def pool(tmp_path, clock=lambda: 10000):
    nodes = [
        {
            "address": f"1.1.1.{i}",
            "port": 443,
            "credential": "00000000-0000-0000-0000-000000000001",
            "parameters": {
                "type": ["ws"],
                "security": ["tls"],
                "sni": ["example.com"],
                "host": ["example.com"],
            },
        }
        for i in range(1, 4)
    ]
    root = tmp_path / "xhs-session"
    root.mkdir(exist_ok=True)
    atomic_private_json(root / "nodes.json", {"nodes": nodes})
    config = root / "proxy.json"
    atomic_private_json(
        config,
        {
            "binary": "/unused/xray",
            "nodes_file": str(root / "nodes.json"),
            "batch_size": 80,
        },
    )
    return ProxyPool(root, config, clock=clock)


def test_eightieth_success_rotates_before_next_network_request_and_restart_keeps_count(
    tmp_path,
):
    p = pool(tmp_path)
    original = p.data["current"]
    for i in range(79):
        p.before_request()
        p.complete(str(i), p.data["generation"])
    p = ProxyPool(p.root, p.config_path)
    assert p.data["completed"] == 79 and p.data["current"] == original
    assert not p.complete("0", 1)
    p.before_request()
    assert p.data["current"] == original
    p.complete("last", p.data["generation"])
    assert p.data["completed"] == 80
    p.before_request()
    assert p.data["current"] != original and p.data["completed"] == 0
    assert p.data["total_completed"] == 80
    assert p.data["failed"] == {}


def test_network_failure_waits_then_uses_only_one_backup(tmp_path):
    now = [10000]
    p = pool(tmp_path, lambda: now[0])
    original = p.data["current"]
    for delay in (60, 300):
        p.failed("XHS_NETWORK_ERROR")
        p = ProxyPool(p.root, p.config_path, clock=lambda: now[0])
        assert p.data["current"] == original
        with pytest.raises(ProviderUnavailable, match="COOLDOWN"):
            p.before_request()
        now[0] += delay
        p.before_request()
    p.failed("XHS_NETWORK_ERROR")
    assert p.data["current"] == p.ids[1] and p.data["backup_used"]
    p.failed("XHS_NETWORK_ERROR")
    p = ProxyPool(p.root, p.config_path, clock=lambda: now[0])
    with pytest.raises(ProviderUnavailable, match="PAUSED"):
        p.before_request()
    assert p.data["current"] == p.ids[1]


def test_generation_fence_and_acceptance_limit(tmp_path):
    p = pool(tmp_path)
    p.complete("first", 1)
    p.select(p.ids[1], "batch_complete")
    with pytest.raises(ProviderUnavailable, match="STALE_GENERATION"):
        p.complete("late", 1)
    settings = json.loads(p.config_path.read_text())
    settings["acceptance_limit"] = 1
    atomic_private_json(p.config_path, settings)
    with pytest.raises(ProviderUnavailable, match="ACCEPTANCE_PAUSED"):
        p.before_download()
    settings["acceptance_limit"] = 5
    atomic_private_json(p.config_path, settings)
    p.before_download()
    assert p.data["total_completed"] == 1


def test_proxy_cannot_be_bypassed_by_environment(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    request = urllib.request.Request("https://cdn.xhscdn.com/video.mp4")
    handler = RequiredProxy("http://127.0.0.1:18998")
    handler.proxy_open(request, "http://127.0.0.1:18998", "https")
    assert request.host == "127.0.0.1:18998"
    assert request._tunnel_host == "cdn.xhscdn.com"


def test_all_entrypoints_obey_pacing_and_challenge(tmp_path):
    p = SessionPolicy(tmp_path, paced=False, clock=lambda: 10000)
    p.reserve("note")
    with pytest.raises(ProviderUnavailable, match="DEFERRED"):
        p.reserve("note")
    p.failed("media", failure("XHS_VERIFICATION_REQUIRED"))
    for entry in ["note", "favorites", "following", "owner", "media"]:
        with pytest.raises(ProviderUnavailable, match="VERIFICATION"):
            p.reserve(entry)
    p.success("media")
    p.failed("favorites", failure("XHS_RATE_LIMITED", delay=9000))
    assert p.download_gate()[1] == 19000
    with pytest.raises(ProviderUnavailable, match="RATE_LIMITED"):
        p.reserve("note")


def test_image_bundle_resumes_partial_files_and_counts_once_after_live_photo(
    tmp_path, monkeypatch
):
    from rag_favorite import xhs_media_service as media

    p = pool(tmp_path)
    v = VideoConfig(
        root=tmp_path / "data",
        credentials_file=tmp_path / "unused",
        xhs_proxy_config=p.config_path,
    )
    s = SessionService(v)
    s.policy.data["recovery"].update(phase="observation", observation_started_at=0)
    s.proxy.data["node_validated_generation"] = s.proxy.data["generation"]
    s.proxy.ensure_started = lambda: False
    s.read = AsyncMock(return_value="a" * 24)
    calls = []

    def image(url, target, **kw):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(url.encode())
        calls.append(url)
        return {"bytes": target.stat().st_size, "sha256": digest_file(target)}

    attempts = [0]

    def video(video, note, **kw):
        attempts[0] += 1
        if attempts[0] == 1:
            raise failure("XHS_NETWORK_ERROR")
        root = video.root / "assets"
        root.mkdir(exist_ok=True)
        target = root / ("1" * 32)
        target.write_bytes(b"validated video")
        return {
            "asset_id": target.name,
            "bytes": target.stat().st_size,
            "sha256": digest_file(target),
        }

    monkeypatch.setattr(media, "download_image", image)
    monkeypatch.setattr(media, "download_note_video", video)
    params = {
        "job_id": str(uuid4()),
        "note": {
            "note_id": "a" * 24,
            "images": [
                {
                    "image_index": 0,
                    "url": "image",
                    "live_photo": True,
                    "live_video_url": "video",
                }
            ],
        },
    }

    async def run():
        with pytest.raises(ProviderUnavailable, match="NETWORK_ERROR"):
            await s.dispatch("download_media", params)
        assert s.proxy.data["total_completed"] == 0
        result = await s.dispatch("download_media", params)
        assert result["complete"] and result["live"]["0"]
        assert s.proxy.data["total_completed"] == 1
        await s.dispatch("download_media", params)
        assert s.proxy.data["total_completed"] == 1

    asyncio.run(run())
    assert calls == ["image"] and attempts[0] == 2


def test_no_proxy_service_never_downloads_directly(tmp_path):
    service = SessionService(SimpleNamespace(root=tmp_path / "data"))
    with pytest.raises(ProviderUnavailable, match="PROXY_UNAVAILABLE"):
        asyncio.run(service.dispatch("download_media", {}))


def test_proxy_public_status_excludes_configuration(tmp_path):
    status = json.dumps(pool(tmp_path).status())
    for private in ["example.com", "credential", "1.1.1.", "/unused"]:
        assert private not in status


def test_restart_reconciles_validated_receipt_before_rotating(tmp_path):
    p = pool(tmp_path)
    for i in range(79):
        p.complete(str(i), 1)
    derived = tmp_path / "data/derived/job"
    derived.mkdir(parents=True)
    atomic_private_json(
        derived / "download-bundle.json",
        {
            "complete": True,
            "downloaded": True,
            "generation": 1,
            "note_id": "last",
        },
    )
    p = ProxyPool(p.root, p.config_path)
    assert p.reconcile(tmp_path / "data") == 1
    assert p.data["completed"] == 80
    assert p.reconcile(tmp_path / "data") == 0
    p.before_request()
    assert p.data["current"] == p.ids[1]


def test_switch_rebinds_port_with_completed_connections_in_time_wait(tmp_path):
    import socket

    p = pool(tmp_path)
    binary = tmp_path / "proxy-fixture"
    binary.write_text("""#!/usr/bin/env python3
import json, socket, sys
if '-test' in sys.argv:
    sys.exit(0)
config = json.load(open(sys.argv[sys.argv.index('-config') + 1]))
sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(('127.0.0.1', config['inbounds'][0]['port']))
sock.listen()
while True:
    client, _ = sock.accept()
    client.sendall(b'fixture')
    client.close()
""")
    binary.chmod(0o700)
    p.binary = binary
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        p.port = available.getsockname()[1]
    try:
        p.ensure_started()
        first_pid = p.process.pid
        with socket.create_connection(("127.0.0.1", p.port)) as client:
            while client.recv(128):
                pass
        p.select(p.ids[1], "batch_complete")
        assert not p.status()["ready"]
        assert p.ensure_started()
        assert p.status()["ready"] and p.process.pid != first_pid
    finally:
        p.stop()
