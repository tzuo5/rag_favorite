from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from backend.ingestion.notifier import TelegramNotifier

logger = logging.getLogger(__name__)


class LoginNotifier:
    def __init__(
        self,
        chat_ids: set[str] | frozenset[str],
        notifier: TelegramNotifier | None = None,
    ):
        self.chat_ids = tuple(sorted(str(item) for item in chat_ids))
        self.notifier = notifier or TelegramNotifier()

    def send_qr(self, path: Path) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for chat_id in self.chat_ids:
            try:
                message_id = self.notifier.photo(
                    chat_id,
                    path,
                    (
                        "🔐 哔哩哔哩登录已失效，相关录入已安全暂停。\n"
                        "请使用哔哩哔哩 App 扫描并确认登录。"
                        "扫码成功后，当前录入会自动继续。二维码仅短期有效。"
                    ),
                )
                results.append({"chat_id": chat_id, "message_id": message_id})
            except Exception:
                logger.exception(
                    "Bilibili login QR delivery failed",
                    extra={"telegram_chat_id": chat_id},
                )
        return results

    def login_succeeded(self, messages: list[dict[str, Any]]) -> None:
        for item in messages:
            chat_id = str(item["chat_id"])
            try:
                self.notifier.progress(
                    chat_id,
                    "✅ 哔哩哔哩登录已恢复并通过真实探测，"
                    "暂停的录入正在自动继续。",
                )
                if item.get("message_id") is not None:
                    self.notifier.delete_message(chat_id, item["message_id"])
            except Exception:
                logger.exception(
                    "Bilibili login success notification failed",
                    extra={"telegram_chat_id": chat_id},
                )
