import io
import json
import urllib.error
from dataclasses import replace
from email.message import Message

import pytest

from rag_favorite.video_config import VideoConfig
from rag_favorite.video_provider import CCRProvider, ProviderUnavailable


class Stream(io.BytesIO):
    def __init__(self, events, headers=None):
        super().__init__(
            b"".join(b"data: " + json.dumps(e).encode() + b"\n" for e in events)
        )
        self.headers = headers or {}
        self.status = 200


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_CCR_API_KEY", "private-test-token")
    return CCRProvider(
        VideoConfig(root=tmp_path, credentials_file=tmp_path / "secret"),
        tmp_path / "calls.jsonl",
    )


@pytest.mark.parametrize("status", [400, 401, 429, 502, 503, 504, 507])
def test_http_errors_are_recorded_without_client_retries(provider, monkeypatch, status):
    calls = []
    headers = Message()
    headers["x-request-id"] = "test-request"
    headers["x-ccr-fallback-attempts"] = "3"
    headers["x-ccr-fallback-failures"] = "503,507,503"
    error = b"Authorization: Bearer private-test-token; full prompt; data:image/jpeg;base64,AAAA"

    def fail(req, timeout):
        calls.append(req)
        raise urllib.error.HTTPError(
            req.full_url, status, "failure", headers, io.BytesIO(error)
        )

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(ProviderUnavailable) as caught:
        provider.json("instructions", "full prompt")
    assert len(calls) == 1
    assert caught.value.code == "CCR_REQUEST_FAILED"
    assert caught.value.diagnostics["http_status"] == status
    assert caught.value.status_code == status
    assert caught.value.retryable is (status in {502, 503, 504})
    assert caught.value.error_category == "http"
    row = provider.calls[-1]
    assert row["failure_category"] == "http"
    assert row["ccr_attempts"] == 3
    assert row["request_id"] == "test-request"
    assert row["request_bytes"] == len(calls[0].data)
    assert row["completed"] is False and row["json_valid"] is False
    assert row["retryable"] is (status in {502, 503, 504})
    saved = provider.ledger.read_text()
    assert "private-test-token" not in saved and "full prompt" not in saved
    assert "AAAA" not in saved
    assert provider.ledger.stat().st_mode & 0o777 == 0o600


def test_completed_invalid_json_is_different_from_stream_failure(provider, monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: Stream(
            [
                {"type": "response.output_text.delta", "delta": "not json"},
                {
                    "type": "response.completed",
                    "response": {"usage": {"total_tokens": 3}},
                },
            ]
        ),
    )
    with pytest.raises(ProviderUnavailable) as caught:
        provider.json("instructions", "input")
    assert caught.value.code == "CCR_RESPONSE_INVALID_JSON"
    assert provider.calls[-1]["completed"] is True
    assert provider.calls[-1]["failure_category"] == "invalid_json"
    assert provider.calls[-1]["json_valid"] is False
    assert caught.value.retryable is False


@pytest.mark.parametrize(
    "events,code,category",
    [
        (
            [
                {
                    "type": "response.incomplete",
                    "response": {"incomplete_details": {"reason": "max_output_tokens"}},
                }
            ],
            "CCR_RESPONSE_INCOMPLETE",
            "stream_incomplete",
        ),
        (
            [{"type": "response.output_text.delta", "delta": "{}"}],
            "CCR_RESPONSE_TRUNCATED",
            "stream_truncated",
        ),
    ],
)
def test_stream_failures_are_observable(provider, monkeypatch, events, code, category):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: Stream(events))
    with pytest.raises(ProviderUnavailable) as caught:
        provider.json("instructions", "input")
    assert caught.value.code == code
    assert provider.calls[-1]["failure_category"] == category
    assert not provider.calls[-1]["completed"]
    assert caught.value.retryable is (code == "CCR_RESPONSE_TRUNCATED")


def test_timeout_and_legacy_records(provider, monkeypatch):
    provider.ledger.write_text('{"images": 1, "seconds": 2}\n')
    provider = CCRProvider(provider.config, provider.ledger)

    def fail(*_a, **_k):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(ProviderUnavailable) as caught:
        provider.json("instructions", "input")
    assert provider.calls[-1]["failure_category"] == "timeout"
    assert caught.value.retryable is True
    assert CCRProvider(provider.config, provider.ledger).visual_requests == 1


def test_error_summary_is_bounded_and_existing_permissions_are_corrected(
    provider, monkeypatch
):
    provider.ledger.touch(mode=0o644)
    provider.ledger.chmod(0o644)

    def fail(req, **_k):
        raise urllib.error.HTTPError(
            req.full_url, 503, "failure", {}, io.BytesIO(("错" * 3000).encode())
        )

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(ProviderUnavailable):
        provider.json("instructions", "input")
    assert len(provider.calls[-1]["error_summary"].encode()) <= 2048
    assert provider.ledger.stat().st_mode & 0o777 == 0o600


def test_success_and_content_repair_kind(provider, monkeypatch):
    from rag_favorite.video_provider import provider_call_kind

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_a, **_k: Stream(
            [
                {"type": "response.output_text.delta", "delta": '{"facts": []}'},
                {"type": "response.completed", "response": {}},
            ]
        ),
    )
    with provider_call_kind(provider, "content_repair"):
        assert provider.json("instructions", "input") == {"facts": []}
    assert provider.calls[-1]["call_kind"] == "content_repair"
    assert provider.calls[-1]["http_status"] == 200
    assert provider.calls[-1]["json_valid"] is True
    provider.json("instructions", "input")
    assert provider.calls[-1]["call_kind"] == "initial"


def test_expired_budget_does_not_send_request(provider, monkeypatch):
    provider.config = replace(provider.config, visual_budget_seconds=1)
    provider.start_visual_budget()
    provider.visual_started -= 2
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_a, **_k: pytest.fail("request after budget")
    )
    with pytest.raises(ProviderUnavailable, match="TIME_BUDGET"):
        provider.json("instructions", "input")
    assert provider.calls == []


def test_malformed_stream_and_interrupt_remain_observable(provider, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: Stream([[]]))
    with pytest.raises(ProviderUnavailable):
        provider.json("instructions", "input")
    assert provider.calls[-1]["failure_category"] == "stream_decode"

    def interrupted(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr("urllib.request.urlopen", interrupted)
    with pytest.raises(KeyboardInterrupt):
        provider.json("instructions", "input")
    assert provider.calls[-1]["failure_category"] == "interrupted"
    assert provider.calls[-1]["completed_at"]


def test_nullable_response_metadata_does_not_fail_extraction(provider, monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_a, **_k: Stream(
            [
                {"type": "response.output_text.delta", "delta": "{}"},
                {
                    "type": "response.completed",
                    "response": {"reasoning": None, "usage": None},
                },
            ]
        ),
    )
    assert provider.json("instructions", "input") == {}
    assert provider.calls[-1]["json_valid"] is True
    assert provider.calls[-1]["reasoning_effort"] is None


def test_network_error_after_completed_is_not_read(provider, monkeypatch):
    class LateFailure(Stream):
        def __iter__(self):
            yield b'data: {"type":"response.output_text.delta","delta":"{}"}\n'
            yield b'data: {"type":"response.completed","response":{"usage":{"total_tokens":9}}}\n'
            raise OSError("connection closed after complete")

    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: LateFailure([]))
    assert provider.json("instructions", "input") == {}
    assert provider.calls[-1]["completed"] is True
    assert provider.calls[-1]["usage"] == {"total_tokens": 9}


def test_provider_enriches_call_without_exposing_request_content(provider, monkeypatch):
    from rag_favorite.model_activity import job_activity, read_calls

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_a, **_k: Stream(
            [
                {"type": "response.output_text.delta", "delta": "{}"},
                {"type": "response.completed", "response": {}},
            ]
        ),
    )
    image = provider.config.root / "test.jpg"
    image.write_bytes(b"image")
    with job_activity(provider.config, "test-job"):
        provider.json("secret instructions", "private transcript", [image])
    row = next(iter(read_calls(provider.config).values()))
    assert row["http_status"] == 200 and row["stream_complete"]
    assert row["valid_output"] and row["status"] == "success"
    assert row["images"] == 1 and row["image_bytes"] == 5
    assert row["request_bytes"] > row["image_bytes"]
    assert "private transcript" not in json.dumps(row)


def test_llm_slot_covers_transport_but_not_json_parsing(provider, monkeypatch):
    from contextlib import contextmanager

    from rag_favorite import video_provider

    active = []

    @contextmanager
    def slot(config):
        active.append(config)
        try:
            yield
        finally:
            active.pop()

    def response(*_args, **_kwargs):
        assert active == [provider.config]
        return Stream(
            [
                {"type": "response.output_text.delta", "delta": "{}"},
                {"type": "response.completed", "response": {}},
            ]
        )

    def parse(text):
        assert active == []
        return json.loads(text)

    monkeypatch.setattr(video_provider, "llm_slot", slot)
    monkeypatch.setattr(video_provider, "extract_json", parse)
    monkeypatch.setattr(video_provider.urllib.request, "urlopen", response)
    assert provider.json("instructions", "input") == {}


def test_pause_fence_prevents_provider_request(provider, monkeypatch):
    from rag_favorite.pipeline_fence import PipelinePaused, stage_owner

    class Owner:
        def before_operation(self):
            raise PipelinePaused("paused")

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_a, **_k: pytest.fail("HTTP after pause")
    )
    with stage_owner(Owner()), pytest.raises(PipelinePaused):
        provider.json("instructions", "input")
