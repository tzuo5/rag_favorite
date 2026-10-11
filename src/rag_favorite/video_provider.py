"""CCR Responses adapter and a local encoder client; embeddings never leave loopback."""

from __future__ import annotations

import base64
import http.client
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import ConfigError
from .model_activity import activity_call, annotate_call, model_call
from .pipeline_fence import before_operation, llm_slot
from .video_config import VideoConfig


class ProviderUnavailable(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        diagnostics: dict | None = None,
        status_code: int | None = None,
        retryable: bool = False,
        error_category: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics or {}
        self.status_code = status_code
        self.retryable = retryable
        self.error_category = error_category


def _error_summary(value, *, secret="", texts=()):
    """Keep diagnostic messages, without credentials or echoed request content."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = str(value)
    try:
        parsed = json.loads(value)
        error = parsed.get("error", parsed) if isinstance(parsed, dict) else {}
        if isinstance(error, dict):
            value = "; ".join(
                str(error[k])
                for k in ("type", "code", "message", "reason")
                if k in error
            )
    except (ValueError, TypeError):
        pass
    for text in (secret, *texts):
        if text:
            for literal in {text, json.dumps(text, ensure_ascii=False)[1:-1]}:
                value = value.replace(literal, "<REDACTED>")
    value = re.sub(r"data:[^\s\"']+", "<REDACTED_IMAGE>", value, flags=re.IGNORECASE)
    value = re.sub(r"(?i)(bearer\s+)[^\s;,\"']+", r"\1<REDACTED>", value)
    value = re.sub(
        r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|cookie|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s;,\"']+",
        r"\1<REDACTED>",
        value,
    )
    value = re.sub(r"[A-Za-z0-9_+/=-]{80,}", "<REDACTED>", value)
    return value.encode("utf-8")[:2048].decode("utf-8", errors="ignore")


def _response_diagnostics(headers, *, secret="", texts=()):
    headers = {k.lower(): str(v) for k, v in (headers or {}).items()}
    attempts = headers.get("x-ccr-fallback-attempts", "")
    return {
        "request_id": _error_summary(
            headers.get("x-request-id", ""), secret=secret, texts=texts
        ),
        "ccr_attempts": int(attempts) if attempts.isdigit() else None,
        "ccr_failures": _error_summary(
            headers.get("x-ccr-fallback-failures", ""), secret=secret, texts=texts
        ),
        "ccr_delays_ms": _error_summary(
            headers.get("x-ccr-fallback-delays-ms", ""), secret=secret, texts=texts
        ),
    }


@contextmanager
def provider_call_kind(provider, kind):
    """Mark content regeneration independently of gateway transport retries."""
    if not isinstance(provider, CCRProvider):
        yield
        return
    previous = provider.call_kind
    provider.call_kind = kind
    try:
        yield
    finally:
        provider.call_kind = previous


def local_encoder(config: VideoConfig, operation: str, payload: dict) -> dict:
    key = {"transcribe": "ASR", "embed-video": "ImageBind"}.get(operation)
    with activity_call(key, operation):
        return _local_encoder(config, operation, payload)


def _local_encoder(config: VideoConfig, operation: str, payload: dict) -> dict:
    req = urllib.request.Request(
        config.encoder_url + "/" + operation,
        data=None if operation == "health" else json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + config.secret("RAG_ENCODER_TOKEN"),
        },
    )
    try:
        before_operation()
        with urllib.request.urlopen(
            req, timeout=10 if operation == "health" else 600
        ) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = {}
        try:
            detail = json.loads(exc.read(4096))
        except ValueError:
            pass
        if operation == "transcribe" and exc.code == 422:
            raise ProviderUnavailable(
                "No speech detected; supply a transcript asset.",
                code="TRANSCRIPT_REQUIRED_NO_SPEECH",
            ) from exc
        if (
            detail.get("detail")
            == "GPU memory exhausted; retry with the explicit CPU profile."
        ):
            raise ProviderUnavailable(
                "Local video encoder memory exhausted.",
                code="LOCAL_ENCODER_MEMORY_EXHAUSTED",
            ) from exc
        raise ProviderUnavailable(
            "Local video encoder returned an error.", code="LOCAL_ENCODER_HTTP_ERROR"
        ) from exc
    except (OSError, ValueError) as exc:
        raise ProviderUnavailable(
            "Local video encoder is unavailable.", code="LOCAL_ENCODER_UNAVAILABLE"
        ) from exc
    if operation == "health" and result.get("space_id") != config.space_id:
        raise ProviderUnavailable(
            "Local encoder is not serving the configured vector space."
        )
    if operation.startswith("embed"):
        vector = result.get("vector", [])
        if (
            result.get("space_id") != config.space_id
            or len(vector) != 1024
            or any(
                not isinstance(v, (float, int)) or not math.isfinite(v) for v in vector
            )
            or abs(sum(v * v for v in vector) - 1) > 0.01
        ):
            raise ConfigError(
                "Local video encoder returned a mismatched or invalid vector."
            )
    return result


def extract_json(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    result = json.loads(value)
    if not isinstance(result, dict):
        raise TypeError("Expected a JSON object.")
    return result


class CCRProvider:
    def __init__(self, config: VideoConfig, ledger: Path | None = None):
        self.config = config
        self.ledger = ledger
        self.calls: list[dict] = (
            [json.loads(line) for line in ledger.read_text().splitlines() if line]
            if ledger and ledger.exists()
            else []
        )
        self.visual_started: float | None = None
        self.visual_requests = sum(bool(c["images"]) for c in self.calls)
        self.visual_images = sum(c["images"] for c in self.calls)
        self.previous_visual_seconds = sum(
            c["seconds"] for c in self.calls if c["images"]
        )
        self.call_kind = "initial"

    def start_visual_budget(self):
        self.visual_started = time.monotonic()

    def check_budget(self, image_count: int = 0):
        if self.visual_started is None:
            return
        if (
            self.config.visual_budget_seconds > 0
            and self.previous_visual_seconds + time.monotonic() - self.visual_started
            >= self.config.visual_budget_seconds
        ):
            raise ProviderUnavailable("VISUAL_TIME_BUDGET_EXCEEDED")
        if image_count and (
            self.visual_requests >= self.config.visual_request_budget
            or self.visual_images + image_count > self.config.visual_image_budget
        ):
            raise ProviderUnavailable("VISUAL_REQUEST_BUDGET_EXCEEDED")

    @model_call("CCR", "extract")
    def json(
        self, instruction: str, text: str, images: list[Path] | None = None
    ) -> dict:
        self.check_budget(len(images or []))
        if images:
            self.visual_requests += 1
            self.visual_images += len(images)
        content: list[dict] = [{"type": "input_text", "text": text}]
        for path in images or []:
            if path.stat().st_size > 10_000_000:
                raise ValueError("Evidence image is too large.")
            content.append(
                {
                    "type": "input_image",
                    "image_url": "data:image/jpeg;base64,"
                    + base64.b64encode(path.read_bytes()).decode(),
                }
            )
        body = {
            "model": self.config.model,
            "instructions": instruction,
            "input": [{"role": "user", "content": content}],
            "stream": True,
            "store": False,
        }
        client_request_id = str(uuid4())
        secret = self.config.secret("RAG_CCR_API_KEY")
        req = urllib.request.Request(
            self.config.router_url,
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + secret,
                "X-Request-ID": client_request_id,
            },
        )
        started = time.monotonic()
        output, completed, usage = [], False, {}
        diagnostics = {
            "created_at": datetime.now(UTC).isoformat(),
            "client_request_id": client_request_id,
            "call_kind": self.call_kind,
            "request_bytes": len(req.data),
            "http_status": None,
            "json_valid": False,
            "failure_category": "",
            "error_code": "",
            "error_summary": "",
            "image_bytes": sum(path.stat().st_size for path in images or []),
            "retryable": False,
            "request_sent": False,
        }
        try:
            # A stalled network read still times out; elapsed time across video
            # segments and previously saved requests does not stop an unlimited job.
            timeout = 600
            if (
                self.visual_started is not None
                and self.config.visual_budget_seconds > 0
            ):
                timeout = min(
                    timeout,
                    max(
                        1,
                        self.config.visual_budget_seconds
                        - self.previous_visual_seconds
                        - (time.monotonic() - self.visual_started),
                    ),
                )
            with llm_slot(self.config):
                before_operation()
                diagnostics["request_sent"] = True
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    diagnostics["http_status"] = getattr(response, "status", 200)
                    diagnostics.update(
                        _response_diagnostics(
                            getattr(response, "headers", {}),
                            secret=secret,
                            texts=(instruction, text),
                        )
                    )
                    for line in response:
                        self.check_budget()
                        if not line.startswith(b"data: "):
                            continue
                        if line[6:].strip() == b"[DONE]":
                            break
                        event = json.loads(line[6:])
                        if not isinstance(event, dict):
                            raise TypeError("Invalid stream event.")
                        response_data = event.get("response") or {}
                        if not isinstance(response_data, dict):
                            raise TypeError("Invalid stream response.")
                        if event.get("type") == "response.output_text.delta":
                            output.append(event.get("delta", ""))
                        if event.get("type") in {
                            "response.failed",
                            "error",
                            "response.incomplete",
                        }:
                            usage = response_data.get("usage") or {}
                            diagnostics["failure_category"] = "stream_incomplete"
                            reason = (
                                event.get("error")
                                or response_data.get("error")
                                or response_data.get("incomplete_details", {})
                            )
                            diagnostics["error_summary"] = _error_summary(
                                json.dumps(reason),
                                secret=secret,
                                texts=(instruction, text),
                            )
                            raise ProviderUnavailable(
                                "CCR did not complete extraction.",
                                code="CCR_RESPONSE_INCOMPLETE",
                            )
                        if event.get("type") == "response.completed":
                            completed = True
                            usage = response_data.get("usage") or {}
                            diagnostics["response_model"] = response_data.get("model")
                            diagnostics["reasoning_effort"] = (
                                response_data.get("reasoning") or {}
                            ).get("effort")
                            # Completion is terminal. A keep-alive connection or
                            # late transport failure must not delay valid output.
                            break
            if not completed:
                diagnostics["failure_category"] = "stream_truncated"
                raise ProviderUnavailable(
                    "CCR response ended before completion.",
                    code="CCR_RESPONSE_TRUNCATED",
                    retryable=True,
                    error_category="network",
                )
            try:
                result = extract_json("".join(output))
            except (ValueError, TypeError) as exc:
                diagnostics["failure_category"] = "invalid_json"
                raise ProviderUnavailable(
                    "CCR returned invalid extraction JSON.",
                    code="CCR_RESPONSE_INVALID_JSON",
                ) from exc
            diagnostics["json_valid"] = True
            from .pipeline_fence import request_succeeded

            request_succeeded()
            return result
        except urllib.error.HTTPError as exc:
            diagnostics.update(
                _response_diagnostics(
                    exc.headers, secret=secret, texts=(instruction, text)
                )
            )
            diagnostics.update(http_status=exc.code, failure_category="http")
            try:
                diagnostics["error_summary"] = _error_summary(
                    exc.read(8192), secret=secret, texts=(instruction, text)
                )
            except OSError:
                diagnostics["error_summary"] = "HTTP error body unavailable."
            finally:
                exc.close()
            diagnostics["error_code"] = "CCR_REQUEST_FAILED"
            raise ProviderUnavailable(
                f"CCR returned HTTP {exc.code}.",
                code="CCR_REQUEST_FAILED",
                diagnostics=diagnostics,
                status_code=exc.code,
                retryable=exc.code in {502, 503, 504} and not completed,
                error_category="http",
            ) from exc
        except (OSError, http.client.HTTPException, ValueError, TypeError) as exc:
            reason = getattr(exc, "reason", exc)
            diagnostics["failure_category"] = (
                "timeout"
                if isinstance(reason, TimeoutError)
                else "stream_decode"
                if isinstance(exc, (ValueError, TypeError))
                else "network"
            )
            diagnostics["error_summary"] = _error_summary(
                str(reason), secret=secret, texts=(instruction, text)
            )
            diagnostics["error_code"] = "CCR_REQUEST_FAILED"
            self.check_budget()
            raise ProviderUnavailable(
                "CCR request failed; verify the local router and its model account.",
                code="CCR_REQUEST_FAILED",
                diagnostics=diagnostics,
                status_code=diagnostics["http_status"],
                retryable=diagnostics["failure_category"] in {"network", "timeout"}
                and not completed,
                error_category=diagnostics["failure_category"],
            ) from exc
        except ProviderUnavailable as exc:
            diagnostics["error_code"] = exc.code
            if not diagnostics["failure_category"]:
                diagnostics["failure_category"] = "budget"
            exc.diagnostics = diagnostics
            exc.status_code = diagnostics["http_status"]
            exc.error_category = diagnostics["failure_category"]
            raise
        except KeyboardInterrupt:
            diagnostics.update(failure_category="interrupted", error_code="INTERRUPTED")
            raise
        finally:
            diagnostics["retryable"] = not completed and (
                diagnostics["failure_category"]
                in {"network", "timeout", "stream_truncated"}
                or diagnostics["failure_category"] == "http"
                and diagnostics["http_status"] in {502, 503, 504}
            )
            diagnostics["status_code"] = diagnostics["http_status"]
            diagnostics["error_category"] = diagnostics["failure_category"]
            if diagnostics["request_sent"]:
                annotate_call(
                    http_status=diagnostics["http_status"],
                    stream_complete=completed,
                    valid_output=diagnostics["json_valid"],
                    request_bytes=diagnostics["request_bytes"],
                    image_bytes=diagnostics["image_bytes"],
                    images=len(images or []),
                    error_category=diagnostics["error_category"],
                    retryable=diagnostics["retryable"],
                    request_id=client_request_id,
                )
            diagnostics["completed_at"] = datetime.now(UTC).isoformat()
            self.calls.append(
                {
                    "model": self.config.model,
                    "images": len(images or []),
                    "seconds": round(time.monotonic() - started, 3),
                    "usage": usage,
                    "completed": completed,
                    **diagnostics,
                }
            )
            if self.ledger:
                self.ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(self.ledger, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a") as f:
                    os.fchmod(f.fileno(), 0o600)
                    f.write(json.dumps(self.calls[-1]) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
