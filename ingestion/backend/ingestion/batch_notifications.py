from __future__ import annotations

from typing import Any


class BatchNotificationDispatcher:
    def __init__(self, repository: Any, notifier: Any):
        self.repository = repository
        self.notifier = notifier

    def run_once(self) -> dict[str, Any] | None:
        notification = self.repository.claim_batch_notification()
        if not notification:
            return None
        payload = notification["payload"]
        previous_message_id = (
            self.repository.latest_batch_notification_message_id(
                notification["batch_id"]
            )
        )
        message_id = self.notifier.progress(payload["chat_id"], payload["text"])
        self.repository.mark_batch_notification_delivered(
            notification["id"], message_id
        )
        if (
            previous_message_id is not None
            and previous_message_id != message_id
        ):
            self.notifier.delete_message(
                payload["chat_id"], previous_message_id
            )
        return notification
