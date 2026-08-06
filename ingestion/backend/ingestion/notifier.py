from __future__ import annotations

import json
import logging
import os
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .models import (
    DESTINATION_CALLBACK_CODES,
    SELECTABLE_DESTINATIONS,
    destination_label,
)

logger = logging.getLogger(__name__)


def normalize_telegram_chat_id(value: str) -> str:
    value = str(value).strip()
    if value.startswith("telegram:"):
        candidate = value.rsplit(":", 1)[-1]
        if candidate.lstrip("-").isdigit():
            return candidate
    return value


class TelegramNotifier:
    def __init__(self, token: str | None = None):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN")

    def _call(
        self, method: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not self.token:
            return None
        payload = dict(payload)
        if "chat_id" in payload:
            payload["chat_id"] = normalize_telegram_chat_id(str(payload["chat_id"]))
        encoded = urllib.parse.urlencode({
            key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            for key, value in payload.items()
        }).encode()
        last_error: Exception | None = None
        for attempt in range(1, 4):
            request = urllib.request.Request(
                f"https://api.telegram.org/bot{self.token}/{method}",
                data=encoded,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    result = json.load(response)
                if not result.get("ok"):
                    raise RuntimeError("Telegram delivery failed")
                logger.info(
                    "telegram notification delivered",
                    extra={"telegram_method": method, "telegram_chat_id": payload.get("chat_id"), "attempt": attempt},
                )
                return result
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < 3:
                    time.sleep(2 ** (attempt - 1))
        raise RuntimeError("Telegram delivery failed after 3 attempts") from last_error

    def _call_with_file(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        field: str,
        path: Path,
    ) -> dict[str, Any] | None:
        if not self.token:
            return None
        boundary = f"codex-{secrets.token_hex(16)}"
        chunks: list[bytes] = []
        for key, value in payload.items():
            normalized = (
                normalize_telegram_chat_id(str(value))
                if key == "chat_id"
                else str(value)
            )
            chunks.extend([
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{key}"'
                    "\r\n\r\n"
                ).encode(),
                normalized.encode("utf-8"),
                b"\r\n",
            ])
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{field}"; '
                'filename="login-qr.png"\r\n'
            ).encode(),
            b"Content-Type: image/png\r\n\r\n",
            path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])
        body = b"".join(chunks)
        last_error: Exception | None = None
        for attempt in range(1, 4):
            request = urllib.request.Request(
                f"https://api.telegram.org/bot{self.token}/{method}",
                data=body,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    result = json.load(response)
                if not result.get("ok"):
                    raise RuntimeError("Telegram delivery failed")
                logger.info(
                    "telegram notification delivered",
                    extra={
                        "telegram_method": method,
                        "telegram_chat_id": payload.get("chat_id"),
                        "attempt": attempt,
                    },
                )
                return result
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < 3:
                    time.sleep(2 ** (attempt - 1))
        raise RuntimeError(
            "Telegram delivery failed after 3 attempts"
        ) from last_error

    @staticmethod
    def _destination_keyboard(job_id: str) -> dict[str, Any]:
        buttons = [
            {
                "text": destination_label(destination),
                "callback_data": (
                    f"vki:{DESTINATION_CALLBACK_CODES[destination]}:{job_id}"
                ),
            }
            for destination in SELECTABLE_DESTINATIONS
        ]
        return {
            "inline_keyboard": [
                buttons[index:index + 2]
                for index in range(0, len(buttons), 2)
            ] + [[{
                "text": "取消",
                "callback_data": f"vki:cancel:{job_id}",
            }]]
        }

    def awaiting_link(
        self,
        chat_id: str,
        job_id: str,
        platform: str,
    ) -> None:
        platform_name = {
            "youtube": "YouTube",
            "bilibili": "Bilibili",
            "xiaohongshu": "小红书",
        }.get(platform, platform or "未知平台")
        self._call("sendMessage", {
            "chat_id": chat_id,
            "text": (
                f"🔗 已识别为 {platform_name} 单个作品链接\n"
                "请选择目标知识库。选择后才会开始读取字幕、"
                "下载或转录。"
            ),
            "reply_markup": self._destination_keyboard(job_id),
        })

    def start_confirmation(
        self,
        chat_id: str,
        request_id: str,
    ) -> None:
        self._call("sendMessage", {
            "chat_id": chat_id,
            "text": (
                "📥 已收到视频、音频或作品链接。\n"
                "点击“开始”后才会读取链接、扫描作者作品、读取字幕、"
                "下载、转录或创建入库任务。"
            ),
            "reply_markup": {
                "inline_keyboard": [[
                    {
                        "text": "开始",
                        "callback_data": f"vks:start:{request_id}",
                    },
                    {
                        "text": "取消",
                        "callback_data": f"vks:cancel:{request_id}",
                    },
                ]]
            },
        })

    def awaiting(self, chat_id: str, job_id: str, title: str, platform: str, author: str | None) -> None:
        self._call("sendMessage", {
            "chat_id": chat_id,
            "text": f"✅ 转录和 Markdown 已完成\n\n标题：{title}\n来源：{platform} · {author or '未知'}\n请选择知识库：",
            "reply_markup": self._destination_keyboard(job_id),
        })

    def progress(self, chat_id: str, text: str) -> int | None:
        result = self._call(
            "sendMessage", {"chat_id": chat_id, "text": text[:500]}
        )
        message = result.get("result") if isinstance(result, dict) else None
        message_id = message.get("message_id") if isinstance(message, dict) else None
        return int(message_id) if message_id is not None else None

    def photo(self, chat_id: str, path: Path, caption: str) -> int | None:
        result = self._call_with_file(
            "sendPhoto",
            {"chat_id": chat_id, "caption": caption[:500]},
            field="photo",
            path=path,
        )
        message = result.get("result") if isinstance(result, dict) else None
        message_id = message.get("message_id") if isinstance(message, dict) else None
        return int(message_id) if message_id is not None else None

    def delete_message(self, chat_id: str, message_id: int | str) -> bool:
        try:
            self._call("deleteMessage", {
                "chat_id": chat_id,
                "message_id": message_id,
            })
            return True
        except RuntimeError:
            logger.warning(
                "could not delete previous telegram batch status",
                extra={
                    "telegram_chat_id": normalize_telegram_chat_id(chat_id),
                    "telegram_message_id": message_id,
                },
                exc_info=True,
            )
            return False

    def completed(self, chat_id: str, destination: str, title: str) -> None:
        name = destination_label(destination)
        self._call("sendMessage", {
            "chat_id": chat_id,
            "text": (
                f"✅ 录入成功\n"
                f"知识库：{name}\n"
                f"标题：{title}\n"
                "Markdown：已写入\n"
                "SQL：已登记\n"
                "向量索引：已完成"
            ),
        })

    def failed(self, chat_id: str, reason: str) -> None:
        self._call("sendMessage", {"chat_id": chat_id, "text": f"处理失败：{reason[:100]}"})

    def discovery_ready(self, discovery: dict[str, Any]) -> None:
        discovered = int(discovery["discovered_count"])
        eligible = int(discovery["eligible_count"])
        skipped = max(0, discovered - eligible)
        platform = {
            "youtube": "YouTube",
            "bilibili": "Bilibili",
            "xiaohongshu": "小红书",
        }.get(str(discovery.get("platform")), str(discovery.get("platform") or "未知"))
        text = (
            "🔎 作者作品预览\n"
            f"平台：{platform}\n"
            f"作者：{discovery.get('author_name') or '未知'}\n"
            f"扫描作品：{discovered} 条\n"
            f"可下载视频：{eligible} 条\n"
            f"跳过非视频/不支持作品：{skipped} 条\n"
            "请选择处理范围。确认前不会下载媒体。"
        )
        markup = None
        if eligible:
            buttons = [
                {
                    "text": f"最近 {limit} 个",
                    "callback_data": f"vkb:range:{limit}:{discovery['id']}",
                }
                for limit in (5, 10, 20, 50)
                if limit <= eligible
            ]
            buttons.append({
                "text": f"下载全部视频（{eligible} 个）",
                "callback_data": (
                    f"vkb:range:all:{eligible}:{discovery['id']}"
                ),
            })
            markup = {
                "inline_keyboard": [
                    buttons[index:index + 2]
                    for index in range(0, len(buttons), 2)
                ]
            }
        request = {"chat_id": discovery["telegram_chat_id"], "text": text}
        if markup:
            request["reply_markup"] = markup
        self._call("sendMessage", request)

    def discovery_queued(self, discovery: dict[str, Any]) -> None:
        platform = {
            "youtube": "YouTube",
            "bilibili": "Bilibili",
            "xiaohongshu": "小红书",
        }.get(
            str(discovery.get("platform")),
            str(discovery.get("platform") or "未知平台"),
        )
        discovery_id = str(discovery["id"])
        self._call("sendMessage", {
            "chat_id": discovery["telegram_chat_id"],
            "text": (
                f"🔎 已收到 {platform} 作者主页\n"
                "正在读取作品列表；确认范围前不会下载或入库。"
            ),
            "reply_markup": {
                "inline_keyboard": [[
                    {
                        "text": "查看进度",
                        "callback_data": (
                            f"vkw:s:d:{discovery_id}"
                        ),
                    },
                    {
                        "text": "暂停",
                        "callback_data": (
                            f"vkw:p:d:{discovery_id}"
                        ),
                    },
                    {
                        "text": "取消",
                        "callback_data": (
                            f"vkw:c:d:{discovery_id}"
                        ),
                    },
                ]],
            },
        })

    def discovery_failed(self, discovery: dict[str, Any]) -> None:
        platform = {
            "youtube": "YouTube",
            "bilibili": "Bilibili",
            "xiaohongshu": "小红书",
        }.get(
            str(discovery.get("platform")),
            str(discovery.get("platform") or "未知平台"),
        )
        reason = str(
            discovery.get("error_message") or "无法读取作者作品列表"
        )[:100]
        self._call("sendMessage", {
            "chat_id": discovery["telegram_chat_id"],
            "text": (
                f"⚠️ {platform} 作者主页读取失败\n"
                f"原因：{reason}\n"
                "这不是后台仍在处理中。若链接可公开访问，请稍后重发；"
                "若平台要求登录，请先更新本机访问凭据。"
            ),
        })
