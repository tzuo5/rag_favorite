"""Read local CCR management state without inference or exposing credentials."""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
MAX_RESPONSE = 1_048_576


class RouterStatusUnavailable(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _endpoint(url):
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in LOCAL_HOSTS
        or parsed.username
        or parsed.password
    ):
        raise RouterStatusUnavailable("CCR management must be local")
    return (
        parsed.scheme,
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )


def _rpc(url, token, method):
    parsed = urlparse(url)
    _endpoint(url)
    request = urllib.request.Request(
        f"{parsed.scheme}://{parsed.netloc}/api/ccr/rpc",
        data=json.dumps({"method": method, "args": []}).encode(),
        headers={"Content-Type": "application/json", "x-ccr-web-auth": token},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=1) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise RouterStatusUnavailable("CCR response too large")
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RouterStatusUnavailable("CCR management unavailable")
    value = result.get("value")
    if not isinstance(value, dict):
        raise RouterStatusUnavailable("CCR response invalid")
    return value


def _condition(condition, requested_model):
    """Tri-state evaluation: never guess about request-dependent conditions."""
    if not isinstance(condition, dict):
        return None
    if condition.get("left") != "request.body.model":
        return None
    right = condition.get("right")
    if not isinstance(right, str):
        return None
    operator = condition.get("operator")
    if operator == "contains":
        return right in requested_model
    if operator == "not-contains":
        return right not in requested_model
    if operator == "starts-with":
        return requested_model.startswith(right)
    if operator == "==":
        return requested_model == right
    if operator == "!=":
        return requested_model != right
    return None


def resolve_route(config, requested_model):
    """Resolve supported model rules from fresh CCR configuration.

    This is the configured route for the next request, not a claim that a model
    inference succeeded. Scripts, profile routing and unknown conditions remain
    explicitly unresolved rather than presenting the request alias as current.
    """
    if config.get("CUSTOM_ROUTER_PATH"):
        raise RouterStatusUnavailable("custom router cannot be resolved")
    profile = config.get("profile", {})
    if profile.get("enabled") and any(
        item.get("enabled") and item.get("routing")
        for item in profile.get("profiles", [])
    ):
        raise RouterStatusUnavailable("profile route cannot be resolved")
    target, effort, rule_name = requested_model, None, "默认模型"
    rules = config.get("Router", {}).get("rules", [])
    for rule in rules:
        if not isinstance(rule, dict):
            raise RouterStatusUnavailable("invalid routing rule")
        if not rule.get("enabled"):
            continue
        if rule.get("type") == "condition":
            matched = _condition(rule.get("condition"), requested_model)
        elif rule.get("type") == "model-prefix":
            pattern = rule.get("pattern")
            matched = (
                requested_model.startswith(pattern)
                if isinstance(pattern, str)
                else None
            )
        else:
            matched = None
        if matched is None:
            raise RouterStatusUnavailable("dynamic route cannot be resolved")
        if not matched:
            continue
        rewrites = rule.get("rewrites") or [rule.get("rewrite")]
        if rewrites == [None] and rule.get("target"):
            rewrites = [
                {
                    "key": "request.body.model",
                    "operation": "set",
                    "value": rule["target"],
                }
            ]
        for rewrite in rewrites:
            if not isinstance(rewrite, dict) or rewrite.get("operation") != "set":
                raise RouterStatusUnavailable("dynamic rewrite cannot be resolved")
            key, value = rewrite.get("key"), rewrite.get("value")
            if key == "request.body.model" and isinstance(value, str):
                target = value
            elif key in {
                "request.body.reasoning.effort",
                "request.body.reasoning_effort",
            } and isinstance(value, str):
                effort = value
            else:
                raise RouterStatusUnavailable("request rewrite cannot be resolved")
        rule_name = rule.get("name", "模型路由规则")
        break  # CCR uses the first matching routing policy.
    if not isinstance(target, str) or len(target) > 200:
        raise RouterStatusUnavailable("invalid target model")
    provider_key, separator, model = target.partition("/")
    providers = config.get("Providers", [])
    matches = [
        item
        for item in providers
        if isinstance(item, dict)
        and (
            (separator and provider_key in {item.get("id"), item.get("name")})
            or (not separator and target in item.get("models", []))
        )
    ]
    if len(matches) != 1:
        raise RouterStatusUnavailable("ambiguous target provider")
    provider = matches[0]
    model = model if separator else target
    if model not in provider.get("models", []):
        raise RouterStatusUnavailable("target model is not configured")
    metadata = provider.get("modelMetadata", {}).get(model, {})
    effort = effort or metadata.get("defaultReasoningLevel")
    if effort not in {
        None,
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    }:
        raise RouterStatusUnavailable("invalid reasoning effort")
    return {
        "name": model,
        "provider": str(provider.get("name", provider_key))[:100],
        "reasoning_effort": effort,
        "route_rule": str(rule_name)[:150],
    }


def read_router_status(video, state_path=None):
    result = {
        "name": "模型信息暂不可用",
        "reasoning_effort": None,
        "requested_model": video.model,
        "source": "Router 实时接口不可用",
        "observed_at": None,
        "live": False,
    }
    path = Path(
        state_path
        or os.environ.get("CCR_SERVICE_STATE")
        or Path.home() / ".claude-code-router/service.json"
    )
    try:
        if path.stat().st_size > 16_384:
            raise RouterStatusUnavailable("invalid service state")
        state = json.loads(path.read_text())
        url = state["url"]
        token = parse_qs(urlparse(url).query).get("ccr_web_token", [""])[0]
        if not token:
            raise RouterStatusUnavailable("missing management authentication")
        gateway = _rpc(url, token, "getGatewayStatus")
        if gateway.get("state") != "running":
            result["source"] = "Router 网关未运行"
            return result
        if _endpoint(gateway.get("endpoint", "")) != _endpoint(video.router_url):
            result["source"] = "Router 网关与当前知识库不匹配"
            return result
        config = _rpc(url, token, "getConfig")
        try:
            route = resolve_route(config, video.model)
        except (RouterStatusUnavailable, TypeError, AttributeError, KeyError):
            result["source"] = "Router 在线 · 动态路由暂无法确定"
            return result
        result.update(route)
        result.update(
            live=True,
            source="Router 实时路由",
            observed_at=datetime.now(UTC).isoformat(),
        )
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        pass  # Credentials, management URLs and provider payloads stay private.
    return result
