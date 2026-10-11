"""Read the live owner queue and control scheduling without model inference."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import UUID

from .config import ConfigError
from .database import connect_database
from .model_activity import read_activity, read_calls
from .queue_control import read_control, set_control
from .router_status import read_router_status
from .video_retention import retry_job
from .video_store import library_id

WORKER_UNIT = "rag-favorite-video-worker.service"
STATES = {
    "queued": "等待中",
    "running": "运行中",
    "published": "清理中",
    "complete": "已完成",
    "blocked": "受阻",
    "failed": "失败",
    "duplicate": "已合并",
}
PIPELINE_STAGES = {
    "download": "下载",
    "prepare": "本地准备",
    "llm": "LLM",
    "publish": "发布",
}


def _call_records(video):
    """Expose known telemetry fields only; never include model payloads."""
    fields = {
        "call_id",
        "model",
        "job_id",
        "pid",
        "status",
        "operation",
        "started_at",
        "updated_at",
        "completed_at",
        "duration_seconds",
        "http_status",
        "stream_complete",
        "valid_output",
        "request_bytes",
        "image_bytes",
        "images",
        "error_category",
        "retryable",
        "request_id",
    }
    calls = read_calls(video)
    return sorted(
        (
            {key: value for key, value in call.items() if key in fields}
            for call in calls.values()
        ),
        key=lambda call: call.get("updated_at", ""),
        reverse=True,
    )


def _call_metrics(calls, now=None):
    """Rates use retained, annotated terminal CCR attempts from the last 24 hours."""
    now = now or datetime.now(UTC)
    since = now - timedelta(hours=24)
    terminal = []
    unknown = 0
    for call in calls:
        if call.get("model") != "CCR" or call.get("status") not in {
            "success",
            "failure",
            "interrupted",
        }:
            continue
        try:
            stamp = datetime.fromisoformat(
                call.get("completed_at") or call["updated_at"]
            )
            if stamp.tzinfo is None or not since <= stamp <= now:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        if not any(
            key in call for key in ("http_status", "valid_output", "error_category")
        ):
            unknown += 1
            continue
        terminal.append(call)
    http_success = sum(
        type(call.get("http_status")) is int and 200 <= call["http_status"] < 300
        for call in terminal
    )
    valid_outputs = sum(
        call.get("stream_complete") is True and call.get("valid_output") is True
        for call in terminal
    )
    return {
        "window_start": since.isoformat(),
        "window_end": now.isoformat(),
        "scope": "retained_completed_ccr_calls",
        "retained_calls": len(calls),
        "attempts": len(terminal),
        "http_success": http_success,
        "valid_outputs": valid_outputs,
        "unknown_attempts": unknown,
    }


def _searchable_completed(db, config, video):
    """Count complete source jobs with persisted vectors in the searchable index."""
    names = (
        (
            "rag_library_state",
            "rag_knowledge_documents",
            "rag_document_summaries",
            "rag_document_summary_chunks",
        )
        if config.unified
        else ("rag_knowledge_summaries", "rag_summary_chunks")
    )
    available = db.execute(
        "SELECT " + ",".join("to_regclass(%s)" for _ in names),
        tuple("public." + name for name in names),
    ).fetchone()
    if not available or not all(available):
        return None
    if config.unified:
        sql = """SELECT count(DISTINCT j.id) FROM public.rag_video_jobs j
            JOIN public.rag_knowledge_documents d ON d.source_job_id=j.id AND d.library_id=j.library_id
            JOIN public.rag_library_state l ON l.library_id=j.library_id
            JOIN public.rag_document_summaries s ON s.document_id=d.id
                AND s.library_id=l.library_id AND s.generation=l.active_generation
            WHERE j.library_id=%s AND j.state='complete' AND EXISTS (
                SELECT 1 FROM public.rag_document_summary_chunks c
                WHERE c.document_id=s.document_id AND c.library_id=s.library_id
                    AND c.generation=s.generation)"""
    else:
        sql = """SELECT count(DISTINCT j.id) FROM public.rag_video_jobs j
            JOIN public.rag_knowledge_summaries s ON s.job_id=j.id AND s.library_id=j.library_id
            WHERE j.library_id=%s AND j.state='complete' AND EXISTS (
                SELECT 1 FROM public.rag_summary_chunks c WHERE c.job_id=s.job_id)"""
    return db.execute(sql, (library_id(video),)).fetchone()[0]


def _pipeline_snapshot(config, video):
    # Old installations remain readable before the additive migration is applied.
    try:
        from .video_pipeline import pipeline_snapshot
    except ImportError:
        return {"available": False, "stages": {}, "running": [], "jobs": []}
    return pipeline_snapshot(config, video)


def describe_stage(stage: str) -> tuple[str, str, str]:
    """Return a label, the involved model, and measured local progress."""
    if stage.startswith("failed:"):
        label, model, progress = describe_stage(stage.removeprefix("failed:"))
        return "失败 · " + label, model, progress
    frames = re.fullmatch(r"extract:(\d+):frames:(\d+)/(\d+)", stage)
    if frames:
        segment, completed, total = map(int, frames.groups())
        return "视觉分析", "CCR", f"段 {segment + 1} · {completed}/{total} 帧"
    transcription = re.fullmatch(r"transcript:(\d+)/(\d+)", stage)
    if transcription:
        completed, total = map(int, transcription.groups())
        return "音频转写", "ASR", f"{completed}/{total} 音频片段"
    segment = re.fullmatch(r"embed_video:(\d+)", stage)
    if segment:
        return "视频向量化", "ImageBind", "段 " + str(int(segment[1]) + 1)
    if stage.startswith("embed_text"):
        return "文字向量化", "Embedding", "—"
    if stage.startswith("review:"):
        return "视觉数值复核", "CCR", "—"
    segment = re.fullmatch(r"extract:(\d+)", stage)
    if segment:
        return "知识提取", "CCR", "段 " + str(int(segment[1]) + 1)
    return {
        "queued": ("等待领取", "—", "—"),
        "recovered": ("恢复检查点", "—", "—"),
        "retry_wait": ("等待重试", "—", "60 秒退避"),
        "fetch_source": ("读取来源", "—", "—"),
        "transcript": ("音频转写", "ASR", "—"),
        "publish": ("写入知识库", "—", "—"),
        "cleanup": ("清理受管文件", "—", "—"),
        "cleanup_failed": ("等待清理重试", "—", "—"),
        "complete": ("已完成", "—", "完成"),
        "title_duplicate": ("同标题已合并", "—", "无需重复处理"),
        "text_only": ("图文正文已入库", "Embedding", "完成"),
        "media_expired": ("媒体已过期", "—", "—"),
    }.get(stage, (stage or "等待领取", "—", "—"))


def service_status(unit=WORKER_UNIT):
    try:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=ActiveState,SubState,MainPID",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode:
            return {"ActiveState": "unknown", "SubState": "unavailable", "MainPID": "0"}
        return dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"ActiveState": "unknown", "SubState": "unavailable", "MainPID": "0"}


def worker_supports_control(worker, video):
    """The currently running PID must have published its queue-gate capability."""
    pid = worker.get("MainPID", "0")
    if pid == "0":
        return False
    try:
        value = json.loads((video.root / "queue-worker.json").read_text())
        return str(value["pid"]) == pid and value["queue_control_version"] == 1
    except (OSError, ValueError, TypeError, KeyError):
        return False


def worker_logs():
    """Display only known structured fields, never payloads or signed URLs."""
    try:
        result = subprocess.run(
            [
                "journalctl",
                "--user",
                "-u",
                WORKER_UNIT,
                "-n",
                "80",
                "--no-pager",
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ["系统日志暂不可用。"]
    lines = []
    for line in result.stdout.splitlines():
        try:
            entry = json.loads(line)
            value = json.loads(entry.get("MESSAGE", ""))
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        safe = {
            key: value[key]
            for key in (
                "job_id",
                "state",
                "error_code",
                "queue_control_version",
                "queue_control_error",
            )
            if key in value and isinstance(value[key], (str, int, bool))
        }
        if safe:
            try:
                stamp = (
                    datetime.fromtimestamp(
                        int(entry["__REALTIME_TIMESTAMP"]) / 1_000_000
                    )
                    .astimezone()
                    .strftime("%H:%M:%S")
                )
            except (KeyError, TypeError, ValueError):
                stamp = "--:--:--"
            if "job_id" in safe:
                message = (
                    "任务 "
                    + str(safe["job_id"])[:8]
                    + " · "
                    + (
                        "错误 " + str(safe["error_code"])
                        if "error_code" in safe
                        else STATES.get(safe.get("state"), str(safe.get("state", "")))
                    )
                )
            else:
                message = (
                    "worker 已加载暂停控制"
                    if "queue_control_version" in safe
                    else "暂停设置读取失败"
                )
            lines.append(f"[{stamp}] {message}")
    return lines[-40:] or ["暂无任务日志。"]


def _embedding_health(config):
    settings = config.embedding
    if settings.backend not in {"lmstudio", "ollama"}:
        return None, "连接尚未验证"
    base = settings.url.removesuffix("/v1/embeddings").removesuffix("/api/embed")
    headers = {}
    if settings.backend == "lmstudio" and os.environ.get(settings.api_key_env):
        headers["Authorization"] = "Bearer " + os.environ[settings.api_key_env]
    path = "/api/v1/models" if settings.backend == "lmstudio" else "/api/ps"
    request = urllib.request.Request(base + path, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            content = response.read(262145)
        if len(content) > 262144:
            raise ValueError("oversized model inventory")
        models = json.loads(content).get("models")
        if not isinstance(models, list):
            raise TypeError("invalid model inventory")
        loaded = (
            any(
                instance.get("id") == settings.model
                for model in models
                if isinstance(model, dict) and model.get("type") == "embedding"
                for instance in model.get("loaded_instances", [])
                if isinstance(instance, dict)
            )
            if settings.backend == "lmstudio"
            else any(
                isinstance(model, dict) and model.get("name") == settings.model
                for model in models
            )
        )
        return loaded, "本地 API 在线 · " + ("模型已加载" if loaded else "模型未加载")
    except urllib.error.HTTPError as exc:
        return None, "本地 API 认证失败" if exc.code in {
            401,
            403,
        } else "模型清单暂不可用"
    except (OSError, ValueError, TypeError, AttributeError):
        return None, "模型清单暂不可用"


def _models(config, video, jobs, worker, calls=None):
    router = read_router_status(video)
    activities = read_activity(video)
    calls = _call_records(video) if calls is None else calls
    active = [job for job in jobs if job["state"] == "running"]
    running = (
        worker.get("ActiveState") == "active" and worker.get("SubState") == "running"
    )

    def credential_present(key):
        try:
            return bool(video.secret(key))
        except ConfigError:
            return False

    encoder_configured = credential_present("RAG_ENCODER_TOKEN")
    specifications = [
        (
            "CCR",
            "文本 / 视觉理解",
            router["name"],
            video.router_url,
            credential_present("RAG_CCR_API_KEY"),
        ),
        (
            "ASR",
            "本地音频转写",
            "Faster Whisper · "
            + (
                video.asr_model.name.removeprefix("asr-")
                if video.asr_model
                else "未配置"
            ),
            video.encoder_url,
            bool(encoder_configured and video.asr_model and video.asr_model.is_dir()),
        ),
        (
            "Embedding",
            "文字向量化 · " + config.embedding.backend,
            config.embedding.model,
            config.embedding.url,
            True,
        ),
        (
            "ImageBind",
            "本地视频向量化",
            "ImageBind Huge",
            video.encoder_url,
            bool(
                encoder_configured and video.checkpoint and video.checkpoint.is_file()
            ),
        ),
    ]
    reachable = {}
    for _, _, _, endpoint, _ in specifications:
        parsed = urlparse(endpoint)
        if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
            address = (parsed.hostname, parsed.port or 80)
            if address not in reachable:
                try:
                    with socket.create_connection(address, timeout=0.4):
                        reachable[address] = True
                except OSError:
                    reachable[address] = False
    cards = []
    embedding_loaded, embedding_connection = _embedding_health(config)
    for key, role, name, endpoint, configured in specifications:
        parsed = urlparse(endpoint)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        connection = (
            reachable.get((parsed.hostname, parsed.port or 80)) if local else None
        )
        live_calls = [
            call
            for call in calls
            if call.get("model") == key
            and call.get("status") == "running"
            and str(call.get("pid")) == worker.get("MainPID")
        ]
        involved = next(
            (
                job
                for job in jobs
                if any(call.get("job_id") == job["id"] for call in live_calls)
            ),
            next((job for job in active if job["model"] == key), None),
        )
        state = (
            "未配置"
            if not configured
            else (
                "离线"
                if connection is False
                else (
                    "处理中"
                    if involved and running
                    else "空闲"
                    if connection
                    else "已配置"
                )
            )
        )
        connection_label = (
            "本地端口可达"
            if connection
            else ("本地服务不可达" if connection is False else "连接尚未验证")
        )
        if key == "Embedding" and connection:
            connection_label = embedding_connection
            if embedding_loaded is False:
                state = "未加载"
            elif embedding_loaded is None and not involved:
                state = "待验证"
        if key == "CCR":
            connection_label = router["source"]
            if router["observed_at"]:
                stamp = datetime.fromisoformat(router["observed_at"]).astimezone()
                connection_label += " · " + stamp.strftime("%H:%M:%S")
            if not router["live"] and connection and not involved:
                state = "待验证"
        activity_label = "尚无调用记录"
        entry = activities.get(key)
        if entry:
            history = []
            try:
                if entry.get("last_completed_at"):
                    stamp = datetime.fromisoformat(
                        entry["last_completed_at"]
                    ).astimezone()
                    outcome = {
                        "success": "成功",
                        "failure": "失败",
                        "interrupted": "中断",
                    }.get(entry.get("last_outcome"), "未知")
                    duration = float(entry.get("last_duration_seconds", 0))
                    if 0 <= duration < 86400:
                        history.append(
                            f"最近调用 {stamp:%H:%M:%S} · {outcome} · {duration:.2f} 秒"
                        )
                if entry.get("last_cache_at"):
                    stamp = datetime.fromisoformat(entry["last_cache_at"]).astimezone()
                    history.append(f"最近缓存复用 {stamp:%H:%M:%S}")
                history.append(
                    f"调用 {entry.get('calls', 0)} 次 · 缓存复用 {entry.get('cache_hits', 0)} 次"
                )
                updated = datetime.fromisoformat(entry["updated_at"])
                recent = 0 <= (datetime.now(UTC) - updated).total_seconds() <= 20
                same_worker = str(entry["pid"]) == worker.get("MainPID")
                same_job = involved and entry["job_id"] == involved["id"]
                if connection and configured and running and same_worker:
                    if entry["status"] == "running" and same_job:
                        state = "处理中"
                    elif recent and entry["status"] == "cached":
                        state = "缓存复用"
                    elif recent and entry["status"] == "success":
                        state = "刚完成"
                    elif recent and entry["status"] in {"failure", "interrupted"}:
                        state = "调用失败"
            except (ValueError, TypeError, KeyError, OverflowError):
                pass
            activity_label = "\n".join(history) or "调用记录暂不可用"
        if live_calls and running and connection and configured:
            state = "处理中"
            call_labels = [
                f"{call.get('job_id', '')[:8]} · {call.get('operation', '调用')}"
                for call in live_calls[:4]
            ]
            activity_label = (
                "当前调用 " + "；".join(call_labels) + "\n" + activity_label
            )
        cards.append(
            {
                "key": key,
                "role": role,
                "name": name,
                "state": state,
                "task": involved["title"] if involved and running else "—",
                "connection": connection_label,
                "activity": activity_label,
                "active_calls": live_calls,
                **(router if key == "CCR" else {}),
            }
        )
    return cards


class QueueMonitor:
    def __init__(self, config, video):
        self.config, self.video = config, video

    def snapshot(self):
        control = read_control(self.video)
        with connect_database(self.config, register_pgvector=False) as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            db.execute("SET LOCAL statement_timeout = '5000ms'")
            counts = dict(
                db.execute(
                    "SELECT state,count(*) FROM public.rag_video_jobs WHERE library_id=%s GROUP BY state",
                    (library_id(self.video),),
                ).fetchall()
            )
            paused_pending = db.execute(
                "SELECT count(*) FROM public.rag_video_jobs WHERE library_id=%s AND state='queued' AND id=ANY(%s::uuid[])",
                (library_id(self.video), list(control.paused_jobs)),
            ).fetchone()[0]
            rows = db.execute(
                """SELECT id,collection,state,stage,error_code,attempts,priority,
                   created_at,updated_at,coalesce(payload->>'title',''),
                   coalesce(payload->>'source_note_id',''),
                   coalesce(payload->>'media_expired','false')
                   FROM public.rag_video_jobs WHERE library_id=%s
                   ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'published' THEN 1
                   WHEN 'queued' THEN 2 WHEN 'blocked' THEN 3 WHEN 'failed' THEN 4 ELSE 5 END,
                   CASE WHEN state='queued' THEN priority ELSE 0 END DESC,
                   CASE WHEN state='queued' THEN created_at END ASC,updated_at DESC,id
                   LIMIT 2000""",
                (library_id(self.video),),
            ).fetchall()
            searchable_completed = _searchable_completed(db, self.config, self.video)
        pipeline = _pipeline_snapshot(self.config, self.video)
        phase_jobs = pipeline.get("jobs", [])
        if isinstance(phase_jobs, dict):
            phase_jobs = [dict(value, job_id=key) for key, value in phase_jobs.items()]
        phases = {
            str(entry["job_id"]): entry for entry in phase_jobs if "job_id" in entry
        }
        jobs, rank = [], 0
        for row in rows:
            (
                job_id,
                collection,
                state,
                stage,
                code,
                attempts,
                priority,
                created,
                updated,
                title,
                note_id,
                expired,
            ) = row
            job_id = str(job_id)
            label, model, progress = describe_stage(stage)
            phase = phases.get(job_id, {})
            phase_stage, phase_state = phase.get("stage"), phase.get("status")
            if phase_stage in PIPELINE_STAGES:
                label = PIPELINE_STAGES[phase_stage] + " · " + label
            if phase_state == "retry_wait":
                progress = "等待重试"
            reason = phase.get("error_code") or phase.get("blocked_reason") or code
            paused = job_id in control.paused_jobs
            if state == "queued" and not paused:
                rank += 1
            jobs.append(
                {
                    "id": job_id,
                    "title": title or note_id or job_id[:12],
                    "collection": collection,
                    "state": state,
                    "stage": stage,
                    "stage_label": label,
                    "model": model,
                    "progress": progress,
                    "state_label": "已暂停"
                    if paused and state == "queued"
                    else {
                        "waiting": "等待中",
                        "retry_wait": "重试等待",
                        "blocked": "受阻",
                    }.get(phase_state, STATES.get(state, state)),
                    "paused": paused,
                    "queue_rank": rank if state == "queued" and not paused else None,
                    "error_code": code,
                    "attempts": attempts,
                    "priority": priority,
                    "created_at": created.isoformat(),
                    "updated_at": updated.isoformat(),
                    "media_expired": expired == "true",
                    "pipeline_stage": phase_stage,
                    "pipeline_status": phase_state,
                    "retry_at": phase.get("retry_at"),
                    "blocked_reason": reason,
                    "stage_owner": phase.get("owner"),
                }
            )
        worker = service_status()
        calls = _call_records(self.video)
        metrics = _call_metrics(calls)
        metrics["searchable_completed"] = searchable_completed
        metrics["publication_scope"] = "current_library_active_search_index"
        return {
            "captured_at": datetime.now(UTC).isoformat(),
            "counts": counts,
            "total": sum(counts.values()),
            "paused_pending": paused_pending,
            "jobs": jobs,
            "control": asdict(control),
            "worker": worker,
            "control_supported": worker_supports_control(worker, self.video),
            "models": _models(self.config, self.video, jobs, worker, calls),
            "model_calls": calls,
            "call_metrics": metrics,
            "pipeline": pipeline,
            "logs": worker_logs(),
            "truncated": sum(counts.values()) > len(jobs),
        }

    def _ensure_worker(self):
        result = subprocess.run(
            ["systemctl", "--user", "start", WORKER_UNIT],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise ConfigError("无法启动 worker 服务，请检查用户服务日志。")

    def action(self, name, job_id=None):
        if name == "start":
            self._ensure_worker()
            deadline = time.monotonic() + 10
            while not worker_supports_control(service_status(), self.video):
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
            if not worker_supports_control(service_status(), self.video):
                raise ConfigError("worker 尚未加载暂停控制，请先更新 worker 服务。")
            set_control(self.video, paused=False)
            return "队列已启动；单独暂停的任务仍保持暂停。"
        if name == "stop":
            if not worker_supports_control(service_status(), self.video):
                raise ConfigError("worker 尚未加载暂停控制，请先更新 worker 服务。")
            set_control(self.video, paused=True)
            return "已暂停领取新阶段和新模型调用；正在执行的操作保存结果后暂停。"
        if name not in {"start_job", "stop_job"} or job_id is None:
            raise ValueError("Unknown queue action")
        job_id = str(UUID(job_id))
        with connect_database(self.config, register_pgvector=False) as db:
            row = db.execute(
                "SELECT state FROM public.rag_video_jobs WHERE id=%s AND library_id=%s",
                (job_id, library_id(self.video)),
            ).fetchone()
        if not row or row[0] not in {"queued", "running", "failed", "blocked"}:
            raise ConfigError("任务已完成或不属于当前知识库，请刷新列表。")
        if not worker_supports_control(service_status(), self.video):
            raise ConfigError("worker 尚未加载暂停控制，请先更新 worker 服务。")
        if name == "stop_job":
            set_control(self.video, job_id=job_id, job_paused=True)
            return "任务已暂停；若已开始执行，将完成当前处理后暂停后续重试。"
        if row[0] in {"failed", "blocked"}:
            retry_job(self.config, self.video, job_id)
        set_control(self.video, job_id=job_id, job_paused=False)
        return "任务已恢复等待处理；全局暂停时需点击顶部 Start。"
