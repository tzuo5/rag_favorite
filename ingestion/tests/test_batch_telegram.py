from __future__ import annotations

import uuid

import pytest

from backend.ingestion.batch_presenter import batch_text, callback_payload
from backend.ingestion.notifier import TelegramNotifier


def test_all_batch_callback_payloads_fit_telegram_limit() -> None:
    object_id = str(uuid.uuid4())
    destination_codes = ("tp", "te", "fi", "ca", "sc", "lc", "ge", "co")
    payloads = [
        callback_payload("range", object_id, 10),
        callback_payload("range", object_id, "all", 123456789),
        *[
            callback_payload(action, object_id, destination, 50)
            for action in ("dest", "start")
            for destination in destination_codes
        ],
        *[
            callback_payload(action, object_id, destination, "all", 123456789)
            for action in ("dest", "start")
            for destination in destination_codes
        ],
        callback_payload("status", object_id),
        callback_payload("pause", object_id),
        callback_payload("resume", object_id),
        callback_payload("cancel", object_id),
        callback_payload("cancel_confirm", object_id),
    ]
    assert all(len(f"vkb:{value}".encode()) <= 64 for value in payloads)
    with pytest.raises(ValueError):
        callback_payload("x" * 40, object_id)


def test_discovery_preview_has_bounded_server_side_callback() -> None:
    calls = []
    notifier = TelegramNotifier(token="test")
    notifier._call = lambda method, payload: calls.append((method, payload))
    discovery_id = uuid.uuid4()
    notifier.discovery_ready({
        "id": discovery_id,
        "telegram_chat_id": "telegram:123",
        "author_name": "Test Author",
        "discovered_count": 35,
        "eligible_count": 35,
        "existing_main_count": 3,
        "existing_cooking_count": 1,
    })
    payload = calls[0][1]
    buttons = [
        button
        for row in payload["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert [button["text"] for button in buttons] == [
        "最近 5 个", "最近 10 个", "最近 20 个",
        "下载全部视频（35 个）",
    ]
    assert [button["callback_data"] for button in buttons] == [
        f"vkb:range:5:{discovery_id}",
        f"vkb:range:10:{discovery_id}",
        f"vkb:range:20:{discovery_id}",
        f"vkb:range:all:35:{discovery_id}",
    ]
    assert all(len(button["callback_data"].encode()) <= 64 for button in buttons)
    assert "确认前不会下载媒体" in payload["text"]
    assert "扫描作品：35 条" in payload["text"]
    assert "可下载视频：35 条" in payload["text"]
    assert "跳过非视频/不支持作品：0 条" in payload["text"]


def test_discovery_preview_omits_ranges_larger_than_available_count() -> None:
    calls = []
    notifier = TelegramNotifier(token="test")
    notifier._call = lambda method, payload: calls.append((method, payload))
    discovery_id = uuid.uuid4()
    notifier.discovery_ready({
        "id": discovery_id,
        "telegram_chat_id": "telegram:123",
        "author_name": "Test Author",
        "discovered_count": 8,
        "eligible_count": 8,
        "existing_main_count": 0,
        "existing_cooking_count": 0,
    })
    buttons = [
        button
        for row in calls[0][1]["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert [button["text"] for button in buttons] == [
        "最近 5 个", "下载全部视频（8 个）",
    ]
    assert buttons[-1]["callback_data"] == (
        f"vkb:range:all:8:{discovery_id}"
    )


def test_discovery_preview_explains_why_scanned_and_downloadable_differ() -> None:
    calls = []
    notifier = TelegramNotifier(token="test")
    notifier._call = lambda method, payload: calls.append((method, payload))
    discovery_id = uuid.uuid4()
    notifier.discovery_ready({
        "id": discovery_id,
        "telegram_chat_id": "telegram:123",
        "platform": "xiaohongshu",
        "author_name": "Test Author",
        "discovered_count": 102,
        "eligible_count": 100,
    })
    payload = calls[0][1]
    assert "扫描作品：102 条" in payload["text"]
    assert "可下载视频：100 条" in payload["text"]
    assert "跳过非视频/不支持作品：2 条" in payload["text"]
    assert payload["reply_markup"]["inline_keyboard"][-1][0] == {
        "text": "下载全部视频（100 个）",
        "callback_data": f"vkb:range:all:100:{discovery_id}",
    }


def test_discovery_failure_is_explicitly_delivered() -> None:
    calls = []
    notifier = TelegramNotifier(token="test")
    notifier._call = lambda method, payload: calls.append((method, payload))
    notifier.discovery_failed({
        "telegram_chat_id": "telegram:123",
        "platform": "xiaohongshu",
        "error_message": "无法读取作者作品列表",
    })
    assert calls == [("sendMessage", {
        "chat_id": "telegram:123",
        "text": (
            "⚠️ 小红书 作者主页读取失败\n"
            "原因：无法读取作者作品列表\n"
            "这不是后台仍在处理中。若链接可公开访问，请稍后重发；"
            "若平台要求登录，请先更新本机访问凭据。"
        ),
    })]


def test_discovery_queue_controls_fit_telegram_callback_limit() -> None:
    calls = []
    notifier = TelegramNotifier(token="test")
    notifier._call = lambda method, payload: calls.append((method, payload))
    notifier.discovery_queued({
        "id": uuid.uuid4(),
        "telegram_chat_id": "telegram:123",
        "platform": "bilibili",
    })
    buttons = calls[0][1]["reply_markup"]["inline_keyboard"][0]
    assert [button["text"] for button in buttons] == [
        "查看进度", "暂停", "取消",
    ]
    assert all(
        len(button["callback_data"].encode()) <= 64
        for button in buttons
    )


def test_start_confirmation_precedes_all_processing() -> None:
    calls = []
    notifier = TelegramNotifier(token="test")
    notifier._call = lambda method, payload: calls.append((method, payload))
    request_id = str(uuid.uuid4())

    notifier.start_confirmation("telegram:123", request_id)

    assert calls[0][0] == "sendMessage"
    payload = calls[0][1]
    assert "点击“开始”后才会" in payload["text"]
    assert "下载、转录或创建入库任务" in payload["text"]
    assert payload["reply_markup"]["inline_keyboard"] == [[
        {"text": "开始", "callback_data": f"vks:start:{request_id}"},
        {"text": "取消", "callback_data": f"vks:cancel:{request_id}"},
    ]]
    assert all(
        len(button["callback_data"].encode()) <= 64
        for button in payload["reply_markup"]["inline_keyboard"][0]
    )


def test_batch_status_is_aggregate_and_path_free() -> None:
    text = batch_text({
        "id": uuid.uuid4(),
        "author_name": "Test Author",
        "selected_destination": "tech",
        "state": "RUNNING",
        "total_count": 10,
        "completed_count": 3,
        "skipped_existing_count": 2,
        "failed_count": 0,
        "cancelled_count": 0,
        "running_count": 1,
        "queued_count": 4,
        "started_count": 8,
        "current_title": "这是一个很长很长而且需要被截断的视频标题示例",
        "current_job_state": "TRANSCRIBING",
    })
    assert "进度：8/10（已进入处理）" in text
    assert "已结束：5/10" in text
    assert "处理中：" not in text
    assert "完成：3" in text
    assert "重复：2" in text
    assert "失败：0" in text
    assert "当前处理：这是一个很长很长而且需要被截断的视频…" in text
    assert "视频进度：audio2txt" in text
    assert "/home/" not in text


def test_batch_status_counts_requeued_persistence_as_progress() -> None:
    text = batch_text({
        "id": uuid.uuid4(),
        "author_name": "Test Author",
        "state": "RUNNING",
        "total_count": 275,
        "completed_count": 0,
        "skipped_existing_count": 0,
        "failed_count": 0,
        "cancelled_count": 0,
        "running_count": 1,
        "queued_count": 274,
        "started_count": 27,
        "current_title": "Current video",
        "current_job_state": "TRANSCRIBING",
    })
    assert "进度：27/275（已进入处理）" in text
    assert "已结束：0/275" in text


@pytest.mark.parametrize(
    ("job_state", "expected"),
    [
        ("DOWNLOADING", "video downloading"),
        ("EXTRACTING_SUBTITLES", "parsing audio"),
        ("TRANSCRIBING", "audio2txt"),
        ("BUILDING_MARKDOWN", "embedding"),
        ("ENRICHING_METADATA", "embedding"),
        ("PERSISTING", "embedding"),
    ],
)
def test_batch_status_maps_worker_state_to_video_stage(
    job_state: str, expected: str
) -> None:
    text = batch_text({
        "id": uuid.uuid4(),
        "author_name": "Test Author",
        "selected_destination": "tech",
        "state": "RUNNING",
        "total_count": 1,
        "completed_count": 0,
        "skipped_existing_count": 0,
        "failed_count": 0,
        "cancelled_count": 0,
        "running_count": 1,
        "queued_count": 0,
        "current_title": "A title",
        "current_job_state": job_state,
    })
    assert f"视频进度：{expected}" in text


def test_terminal_batch_progress_is_total_even_with_errors() -> None:
    text = batch_text({
        "id": uuid.uuid4(),
        "author_name": "Test Author",
        "state": "COMPLETED_WITH_ERRORS",
        "total_count": 50,
        "completed_count": 11,
        "skipped_existing_count": 10,
        "failed_count": 29,
        "cancelled_count": 0,
        "running_count": 0,
        "queued_count": 0,
        "started_count": 50,
        "current_title": None,
        "current_job_state": None,
    })
    assert "进度：50/50（已进入处理）" in text
    assert "已结束：50/50" in text
