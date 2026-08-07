from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import PlatformErrorCode


@dataclass(frozen=True)
class ClassifiedPlatformError:
    code: PlatformErrorCode
    safe_message: str
    immediate_open: bool = False
    rate_limited: bool = False


def classify_platform_error(error: BaseException) -> ClassifiedPlatformError:
    structured_code = getattr(error, "code", None)
    try:
        code = PlatformErrorCode(structured_code)
    except (TypeError, ValueError):
        code = None
    if code is not None:
        safe_message = str(
            getattr(error, "safe_message", None)
            or {
                PlatformErrorCode.AUTH_EXPIRED: "平台登录状态已失效",
                PlatformErrorCode.BOT_CHECK: "平台要求人机验证",
                PlatformErrorCode.RATE_LIMITED: "平台请求频率受限",
                PlatformErrorCode.PLATFORM_BLOCKED: "平台暂时阻止了访问",
                PlatformErrorCode.PLATFORM_UNAVAILABLE: "平台服务暂时不可用",
                PlatformErrorCode.NETWORK_ERROR: "平台网络访问失败",
                PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED:
                    "平台读取规则已变化",
                PlatformErrorCode.DISCOVERY_FAILED: "无法读取作者作品列表",
            }[code]
        )
        return ClassifiedPlatformError(
            code,
            safe_message,
            immediate_open=code in {
                PlatformErrorCode.AUTH_EXPIRED,
                PlatformErrorCode.BOT_CHECK,
                PlatformErrorCode.PLATFORM_BLOCKED,
                PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED,
            },
            rate_limited=code in {
                PlatformErrorCode.RATE_LIMITED,
                PlatformErrorCode.PLATFORM_UNAVAILABLE,
            },
        )
    text = " ".join(str(error).split()).lower()
    if any(token in text for token in ("bot check", "confirm you’re not a bot", "confirm you're not a bot", "captcha", "challenge")):
        return ClassifiedPlatformError(
            PlatformErrorCode.BOT_CHECK,
            "平台要求人机验证",
            immediate_open=True,
        )
    if any(token in text for token in (
        "sign in to confirm",
        "login required",
        "cookies are no longer valid",
        "authentication required",
        "cookie has expired",
        "cookie is invalid",
    )):
        return ClassifiedPlatformError(
            PlatformErrorCode.AUTH_EXPIRED,
            "平台登录状态已失效",
            immediate_open=True,
        )
    if "wbi" in text and any(
        token in text for token in ("signature", "sign", "key", "invalid")
    ):
        return ClassifiedPlatformError(
            PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED,
            "Bilibili WBI 签名规则已变化",
            immediate_open=True,
        )
    if any(token in text for token in (
        "x-s signature",
        "x-s-common",
        "invalid signature",
    )):
        return ClassifiedPlatformError(
            PlatformErrorCode.PLATFORM_BLOCKED,
            "小红书签名或风控校验未通过",
            immediate_open=True,
        )
    if re.search(r"\b(?:http error )?(?:412|403)\b", text):
        return ClassifiedPlatformError(
            PlatformErrorCode.PLATFORM_BLOCKED,
            "平台暂时阻止了访问",
            immediate_open=True,
        )
    if "429" in text or "too many requests" in text or "rate limit" in text:
        return ClassifiedPlatformError(
            PlatformErrorCode.RATE_LIMITED,
            "平台请求频率受限",
            rate_limited=True,
        )
    if any(token in text for token in (
        "network is unreachable",
        "name or service not known",
        "temporary failure in name resolution",
        "connection timed out",
        "connection reset",
        "urlopen error",
    )):
        return ClassifiedPlatformError(
            PlatformErrorCode.NETWORK_ERROR,
            "平台网络访问失败",
        )
    if re.search(r"\b5\d\d\b", text) or "temporarily unavailable" in text:
        return ClassifiedPlatformError(
            PlatformErrorCode.PLATFORM_UNAVAILABLE,
            "平台服务暂时不可用",
            rate_limited=True,
        )
    return ClassifiedPlatformError(
        PlatformErrorCode.DISCOVERY_FAILED,
        "无法读取作者作品列表",
    )
