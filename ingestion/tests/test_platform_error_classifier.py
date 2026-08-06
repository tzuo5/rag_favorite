from backend.ingestion.discovery.errors import classify_platform_error
from backend.ingestion.discovery.models import DiscoveryAdapterError
from backend.ingestion.models import PlatformErrorCode


def test_bot_check_takes_precedence_over_generic_sign_in_text() -> None:
    result = classify_platform_error(
        RuntimeError("Sign in to confirm you're not a bot")
    )
    assert result.code == PlatformErrorCode.BOT_CHECK
    assert result.immediate_open is True


def test_rate_limit_and_unknown_errors_have_stable_safe_codes() -> None:
    limited = classify_platform_error(RuntimeError("HTTP Error 429"))
    assert limited.code == PlatformErrorCode.RATE_LIMITED
    assert limited.rate_limited is True
    unknown = classify_platform_error(
        RuntimeError("request failed token=secret raw-response=private")
    )
    assert unknown.code == PlatformErrorCode.DISCOVERY_FAILED
    assert "secret" not in unknown.safe_message

    network = classify_platform_error(
        OSError("urlopen error: network is unreachable token=secret")
    )
    assert network.code == PlatformErrorCode.NETWORK_ERROR
    assert network.safe_message == "平台网络访问失败"


def test_platform_specific_signature_failures_stop_automatic_retries() -> None:
    wbi = classify_platform_error(
        RuntimeError("Bilibili WBI signature key is invalid")
    )
    assert wbi.code == PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED
    assert wbi.immediate_open is True
    assert "WBI" in wbi.safe_message

    xhs = classify_platform_error(
        RuntimeError("x-s-common invalid signature token=private")
    )
    assert xhs.code == PlatformErrorCode.PLATFORM_BLOCKED
    assert xhs.immediate_open is True
    assert "private" not in xhs.safe_message


def test_structured_adapter_bot_check_is_not_downgraded_by_localized_text() -> None:
    result = classify_platform_error(DiscoveryAdapterError(
        PlatformErrorCode.BOT_CHECK.value,
        "小红书要求人机验证",
    ))

    assert result.code == PlatformErrorCode.BOT_CHECK
    assert result.safe_message == "小红书要求人机验证"
    assert result.immediate_open is True
