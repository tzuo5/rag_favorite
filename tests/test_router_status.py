import json
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from rag_favorite.router_status import (
    RouterStatusUnavailable,
    read_router_status,
    resolve_route,
)


def routing(model="gpt-6-luna", effort="xhigh"):
    return {
        "APIKEY": "NEVER_EXPOSE_API_KEY",
        "Providers": [
            {
                "id": "codex-api",
                "name": "Codex API",
                "api_key": "NEVER_EXPOSE_PROVIDER_KEY",
                "models": [model],
                "modelMetadata": {model: {"defaultReasoningLevel": effort}},
            }
        ],
        "Router": {
            "rules": [
                {
                    "enabled": True,
                    "type": "condition",
                    "name": "GPT route",
                    "condition": {
                        "left": "request.body.model",
                        "operator": "contains",
                        "right": "gpt-",
                    },
                    "rewrites": [
                        {
                            "key": "request.body.model",
                            "operation": "set",
                            "value": "codex-api/" + model,
                        },
                        {
                            "key": "request.body.reasoning.effort",
                            "operation": "set",
                            "value": effort,
                        },
                    ],
                }
            ]
        },
    }


@pytest.fixture
def router(tmp_path):
    current = {"config": routing(), "state": "running", "calls": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/api/ccr/rpc"
            assert self.headers["x-ccr-web-auth"] == "NEVER_EXPOSE_MANAGEMENT_TOKEN"
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            current["calls"].append(request["method"])
            if request["method"] == "getGatewayStatus":
                value = {
                    "state": current["state"],
                    "endpoint": current.get("endpoint", url),
                }
            else:
                assert request["method"] == "getConfig"
                value = current["config"]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "value": value}).encode())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state = tmp_path / "service.json"
    state.write_text(
        json.dumps({"url": url + "/?ccr_web_token=NEVER_EXPOSE_MANAGEMENT_TOKEN"})
    )
    video = SimpleNamespace(
        router_url=url + "/v1/responses", model="Codex API/gpt-6.1-sol"
    )
    try:
        yield current, video, state
    finally:
        server.shutdown()
        thread.join(2)
        server.server_close()


def test_real_http_refresh_tracks_changed_model_and_effort_without_restart(router):
    current, video, state = router
    first = read_router_status(video, state)
    assert first["name"] == "gpt-6-luna" and first["reasoning_effort"] == "xhigh"
    assert first["live"] and first["observed_at"]
    current["config"] = routing("gpt-another", "high")
    second = read_router_status(video, state)
    assert second["name"] == "gpt-another" and second["reasoning_effort"] == "high"
    assert second["requested_model"] == "Codex API/gpt-6.1-sol"
    assert "NEVER_EXPOSE" not in json.dumps([first, second])
    assert current["calls"] == ["getGatewayStatus", "getConfig"] * 2


@pytest.mark.parametrize(
    "situation", ["stopped", "different_gateway", "script", "bad_response"]
)
def test_unavailable_or_dynamic_route_does_not_report_request_alias_as_live(
    router, situation
):
    current, video, state = router
    if situation == "stopped":
        current["state"] = "stopped"
    elif situation == "different_gateway":
        current["endpoint"] = "http://127.0.0.1:1"
    elif situation == "script":
        current["config"]["Router"]["rules"][0]["type"] = "script"
    else:
        current["config"] = {"Providers": None}
    result = read_router_status(video, state)
    assert not result["live"]
    assert result["name"] == "模型信息暂不可用"
    assert result["observed_at"] is None


def test_missing_state_is_safe_and_read_only(tmp_path):
    state = tmp_path / "missing.json"
    result = read_router_status(
        SimpleNamespace(model="old-model", router_url="http://127.0.0.1:3456"), state
    )
    assert not result["live"] and not state.exists()


def test_management_credentials_never_sent_to_remote_host(tmp_path):
    state = tmp_path / "service.json"
    state.write_text(
        json.dumps({"url": "https://example.invalid/?ccr_web_token=SECRET"})
    )
    result = read_router_status(
        SimpleNamespace(model="old-model", router_url="http://127.0.0.1:3456"), state
    )
    assert not result["live"] and "SECRET" not in json.dumps(result)


def test_first_matching_route_and_provider_default_reasoning_are_used():
    config = routing()
    second = deepcopy(config["Router"]["rules"][0])
    second["rewrites"][1]["value"] = "low"
    config["Router"]["rules"].append(second)
    assert resolve_route(config, "Codex API/gpt-6.1-sol")["reasoning_effort"] == "xhigh"
    config["Router"]["rules"][0]["rewrites"].pop()
    assert resolve_route(config, "Codex API/gpt-6.1-sol")["reasoning_effort"] == "xhigh"


def test_unknown_earlier_condition_prevents_false_route_resolution():
    config = routing()
    unknown = deepcopy(config["Router"]["rules"][0])
    unknown["condition"]["left"] = "request.headers.user-agent"
    config["Router"]["rules"].insert(0, unknown)
    with pytest.raises(RouterStatusUnavailable):
        resolve_route(config, "Codex API/gpt-6.1-sol")
