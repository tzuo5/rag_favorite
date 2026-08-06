from __future__ import annotations

from typing import Any

from .batch_presenter import batch_text, video_stage_label
from .models import destination_label

TERMINAL_JOB_STATES = {"COMPLETED", "FAILED", "CANCELLED"}
TERMINAL_DISCOVERY_STATES = {
    "CANCELLED", "FAILED", "EXPIRED", "CONSUMED",
}
TERMINAL_BATCH_STATES = {
    "COMPLETED", "COMPLETED_WITH_ERRORS", "CANCELLED", "FAILED",
}


def platform_label(value: object) -> str:
    return {
        "youtube": "YouTube",
        "bilibili": "Bilibili",
        "xiaohongshu": "小红书",
    }.get(str(value or ""), str(value or "未知平台"))


def job_text(job: dict[str, Any]) -> str:
    state = str(job["state"])
    title = job.get("title") or "待识别视频"
    lines = [
        f"🎬 单视频任务 · {str(job['id'])[:8]}",
        f"平台：{platform_label(job.get('source_platform'))}",
        f"标题：{title}",
        f"状态：{state}",
    ]
    if state == "COMPLETED":
        lines.extend([
            f"知识库：{destination_label(job.get('selected_destination'))}",
            "视频进度：已完成",
        ])
    elif state == "FAILED":
        lines.extend([
            "视频进度：失败",
            f"原因：{job.get('last_error_code') or '未知'}",
        ])
    elif state == "CANCELLED":
        lines.append("视频进度：已取消")
    elif state == "PAUSED_USER":
        lines.append("视频进度：已暂停")
    elif state == "AWAITING_DESTINATION":
        lines.append("视频进度：等待选择知识库")
    else:
        lines.append(f"视频进度：{video_stage_label(state)}")
    return "\n".join(lines)


def discovery_text(discovery: dict[str, Any]) -> str:
    state = str(discovery["state"])
    lines = [
        f"🔎 作者列表任务 · {str(discovery['id'])[:8]}",
        f"平台：{platform_label(discovery.get('platform'))}",
        f"作者：{discovery.get('author_name') or '待读取'}",
        f"状态：{state}",
        f"已扫描：{int(discovery.get('discovered_count') or 0)}",
        f"可下载：{int(discovery.get('eligible_count') or 0)}",
    ]
    if state == "QUEUED":
        lines.append("进度：等待读取作者作品列表")
    elif state == "DISCOVERING":
        lines.append("进度：正在读取作者作品列表")
    elif state == "READY":
        lines.append("进度：预览已生成，等待选择范围")
    elif state == "PAUSED_USER":
        lines.append("进度：已暂停")
    elif state == "CANCELLED":
        lines.append("进度：已取消")
    elif state == "FAILED":
        lines.append(
            f"进度：失败（{discovery.get('error_code') or '未知原因'}）"
        )
    else:
        lines.append(f"进度：{state}")
    return "\n".join(lines)


def work_controls(kind: str, state: str) -> list[str]:
    if kind == "job":
        if state == "PAUSED_USER":
            return ["status", "resume", "cancel"]
        if state in TERMINAL_JOB_STATES:
            return ["status"]
        if state == "AWAITING_DESTINATION":
            return ["status", "cancel"]
        return ["status", "pause", "cancel"]
    if kind == "discovery":
        if state == "PAUSED_USER":
            return ["status", "resume", "cancel"]
        if state in TERMINAL_DISCOVERY_STATES:
            return ["status"]
        return ["status", "pause", "cancel"]
    if kind == "batch":
        if state == "PAUSED_USER":
            return ["status", "resume", "cancel"]
        if state in TERMINAL_BATCH_STATES:
            return ["status"]
        if state.startswith("PAUSED_") or state == "CANCELLING":
            return ["status", "cancel"]
        return ["status", "pause", "cancel"]
    return []


def present_work(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    if kind == "batch":
        text = batch_text(row)
    elif kind == "discovery":
        text = discovery_text(row)
    else:
        text = job_text(row)
    return {
        "ok": True,
        "kind": kind,
        "object_id": str(row["id"]),
        "status": str(row["state"]),
        "controls": work_controls(kind, str(row["state"])),
        "text": text,
    }
