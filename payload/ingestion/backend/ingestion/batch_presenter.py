from __future__ import annotations

from typing import Any

_TERMINAL_BATCH_STATES = {
    "COMPLETED",
    "COMPLETED_WITH_ERRORS",
    "CANCELLED",
    "FAILED",
}

_VIDEO_STAGE_LABELS = {
    "RECEIVED": "video downloading",
    "DOWNLOADING": "video downloading",
    "EXTRACTING_SUBTITLES": "parsing audio",
    "TRANSCRIBING": "audio2txt",
    "BUILDING_MARKDOWN": "embedding",
    "ENRICHING_METADATA": "embedding",
    "PERSISTING": "embedding",
}


def video_stage_label(job_state: object, batch_state: object = "") -> str:
    state = str(batch_state or "")
    if state in _TERMINAL_BATCH_STATES:
        return "已完成"
    if state.startswith("PAUSED_"):
        return "已暂停"
    if state == "CANCELLING":
        return "正在取消"
    return _VIDEO_STAGE_LABELS.get(str(job_state or ""), "等待处理")


def _short_title(value: object, limit: int = 18) -> str:
    title = " ".join(str(value or "").split())
    if not title:
        return "暂无"
    return title if len(title) <= limit else f"{title[:limit]}…"


def batch_text(batch: dict[str, Any]) -> str:
    state = batch["state"]
    total = int(batch["total_count"])
    terminal_count = sum(
        int(batch.get(key) or 0)
        for key in (
            "completed_count",
            "skipped_existing_count",
            "failed_count",
            "cancelled_count",
        )
    )
    # Batch children have two processing passes.  After media extraction a
    # child is queued again in PERSISTING, so queued_count cannot distinguish
    # untouched work from work that has already made substantial progress.
    # Repositories now expose the monotonic count of children ever started.
    # Keep the fallback for notification payloads produced by an older worker.
    progress_count = min(
        total,
        int(
            batch.get(
                "started_count",
                terminal_count + int(batch.get("running_count") or 0),
            )
            or 0
        ),
    )
    heading = (
        "📚 作者批次已完成"
        if state in _TERMINAL_BATCH_STATES
        else "📚 作者批次处理中"
    )
    lines = [
        f"{heading} · {str(batch['id'])[:8]}",
        f"作者：{batch.get('author_name') or '未知'}",
        f"状态：{state}",
        f"进度：{progress_count}/{total}（已进入处理）",
        f"已结束：{terminal_count}/{total}",
        f"完成：{batch['completed_count']}",
        f"重复：{batch['skipped_existing_count']}",
        f"失败：{batch['failed_count']}",
        f"当前处理：{_short_title(batch.get('current_title'))}",
        "视频进度："
        f"{video_stage_label(batch.get('current_job_state'), state)}",
    ]
    if str(state).startswith("PAUSED_"):
        lines.append(f"暂停原因：{batch.get('pause_code') or '等待恢复'}")
    return "\n".join(lines)


def callback_payload(action: str, object_id: str, *parts: object) -> str:
    payload = ":".join([action, *(str(part) for part in parts), object_id])
    full = f"vkb:{payload}"
    if len(full.encode("utf-8")) > 64:
        raise ValueError("Telegram callback payload exceeds 64 bytes")
    return payload
