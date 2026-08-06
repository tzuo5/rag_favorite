from __future__ import annotations

import uuid

import pytest

from backend.ingestion.batch_notifications import BatchNotificationDispatcher


class FakeRepository:
    def __init__(self):
        batch_id = uuid.uuid4()
        self.notification = {
            "id": uuid.uuid4(),
            "batch_id": batch_id,
            "payload": {
                "batch_id": str(batch_id),
                "chat_id": "123",
                "text": "batch status",
            },
        }
        self.delivered = []
        self.previous_message_id = 41

    def claim_batch_notification(self):
        value, self.notification = self.notification, None
        return value

    def latest_batch_notification_message_id(self, batch_id):
        return self.previous_message_id

    def mark_batch_notification_delivered(
        self, notification_id, telegram_message_id=None
    ):
        self.delivered.append((notification_id, telegram_message_id))


class FakeNotifier:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []
        self.deleted = []

    def progress(self, chat_id, text):
        self.calls.append((chat_id, text))
        if self.fail:
            raise RuntimeError("telegram unavailable")
        return 42

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True


def test_dispatcher_marks_delivered_only_after_success() -> None:
    repository = FakeRepository()
    notifier = FakeNotifier()
    result = BatchNotificationDispatcher(repository, notifier).run_once()
    assert result["payload"]["text"] == "batch status"
    assert notifier.calls == [("123", "batch status")]
    assert repository.delivered == [(result["id"], 42)]
    assert notifier.deleted == [("123", 41)]


def test_dispatcher_does_not_ack_failed_delivery() -> None:
    repository = FakeRepository()
    notifier = FakeNotifier(fail=True)
    with pytest.raises(RuntimeError):
        BatchNotificationDispatcher(repository, notifier).run_once()
    assert repository.delivered == []
    assert notifier.deleted == []
