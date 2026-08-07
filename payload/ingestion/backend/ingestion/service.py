from __future__ import annotations

import asyncio
import logging
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings
from .destinations import KnowledgeDestinationService, VectorRepository
from .discovery.errors import classify_platform_error
from .discovery.url_classifier import classify_url
from .enrichment import MetadataEnricher
from .markdown import KnowledgeFileBuilder, infer_domain
from .media import (
    CleanupManager,
    MetadataExtractor,
    TranscriptService,
    download_xiaohongshu_cover,
    probe_duration,
    sha256_file,
)
from .models import (
    BatchState,
    Destination,
    JobState,
    Platform,
    SourceMetadata,
    destination_label,
    utc_now,
)
from .notifier import TelegramNotifier
from .repository import JobControlRequested, SqlRepository
from .security import redact_sensitive_text, safe_filename, validate_public_url

logger = logging.getLogger(__name__)


class BatchDeferred(RuntimeError):
    """The child was atomically requeued/cancelled; processing should stop."""


def should_store_video_cover(job: dict, platform: Platform) -> bool:
    return (
        platform == Platform.XIAOHONGSHU
        and job.get("selected_destination") == Destination.COOKING.value
    )


def can_reuse_duplicate_markdown(platform: Platform) -> bool:
    # Xiaohongshu Markdown is destination-sensitive because cooking owns a
    # local cover asset and every other destination must be image-free.
    return platform != Platform.XIAOHONGSHU


def requires_batch_persistence_requeue(job: dict) -> bool:
    return job.get("notification_mode") == "BATCH_SILENT"


def _friendly_error(exc: Exception) -> tuple[str, str]:
    text = str(exc).lower()
    if "private" in text or "内网" in text: return "UNSAFE_URL", "链接指向本机或内网，已拒绝"
    if "duration" in text or "时长" in text: return "DURATION_LIMIT", "视频时长超过限制"
    if "larger" in text or "size" in text or "空间" in text: return "SIZE_LIMIT", "文件过大或磁盘空间不足"
    if "unsupported" in text: return "UNSUPPORTED", "当前平台或媒体格式不受支持"
    if "download" in text or "extractor" in text: return "DOWNLOAD_FAILED", "下载失败；如平台需要登录，请配置 cookies"
    return "PROCESSING_FAILED", "转录或入库失败，请稍后重试"


class VideoIngestionService:
    def __init__(self, settings: Settings | None = None, notifier: TelegramNotifier | None = None):
        self.settings = settings or Settings()
        self.settings.ensure_directories()
        self.sql = SqlRepository(self.settings)
        self.metadata = MetadataExtractor(self.settings)
        self.transcripts = TranscriptService(self.settings)
        self.builder = KnowledgeFileBuilder()
        self.enricher = MetadataEnricher()
        self.cleanup = CleanupManager()
        self.destinations = KnowledgeDestinationService(self.settings, VectorRepository(self.settings))
        self.notifier = notifier or TelegramNotifier()

    def _notify(self, method: str, *args: object) -> None:
        try:
            getattr(self.notifier, method)(*args)
        except Exception:
            # Notification delivery must not roll a completed processing or
            # persistence state backward and trigger expensive duplicate work.
            logger.exception("telegram notification failed", extra={"notification": method})

    def _notify_job(self, job: dict, method: str, *args: object) -> None:
        if job.get("notification_mode") == "BATCH_SILENT":
            return
        self._notify(method, *args)

    async def process(self, job: dict) -> None:
        job_id = str(job["id"])
        job_dir = self.settings.temp_root / job_id
        try:
            if int(job.get("retry_count") or 0) > 0:
                self._notify_job(
                    job,
                    "progress",
                    str(job["telegram_chat_id"]),
                    f"🔄 正在进行第 {int(job['retry_count']) + 1} 次尝试…",
                )
            if job["state"] == "PERSISTING":
                await asyncio.to_thread(self._persist, job)
                return
            job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if job["input_kind"] == "url":
                await self._process_url(job, job_dir)
            else:
                await self._process_media(job, job_dir)
        except BatchDeferred:
            return
        except JobControlRequested as control:
            if control.state == JobState.CANCELLED.value:
                self._cleanup_cancelled_artifacts(job_id)
            return
        except Exception as exc:
            current = self.sql.get_job(job_id) or job
            if current.get("state") in {
                JobState.PAUSED_USER.value,
                JobState.CANCELLED.value,
            }:
                if current["state"] == JobState.CANCELLED.value:
                    self._cleanup_cancelled_artifacts(job_id)
                return
            if (
                job.get("notification_mode") != "BATCH_SILENT"
                and self._defer_individual_platform_auth(current, exc)
            ):
                return
            code, message = _friendly_error(exc)
            safe_error = redact_sensitive_text(exc, max_length=1000)
            logger.exception("job failed", extra={"job_id": job_id, "stage": current.get("state"), "error_code": code, "telegram_user_id": job.get("telegram_user_id")})
            retry_count = int(current.get("retry_count") or 0)
            persisting = current.get("state") == "PERSISTING"
            retryable = code in {"DOWNLOAD_FAILED", "PROCESSING_FAILED"} and retry_count < self.settings.max_retries
            if job.get("notification_mode") == "BATCH_SILENT":
                await asyncio.to_thread(
                    self._handle_batch_failure,
                    current,
                    exc,
                    code,
                    retryable,
                )
                return
            if retryable:
                self.sql.transition(
                    job_id,
                    JobState.PERSISTING if persisting else JobState.RECEIVED,
                    retry_count=retry_count + 1,
                    next_attempt_at=(
                        datetime.now(timezone.utc)
                        + timedelta(seconds=self.settings.job_retry_delay_seconds)
                    ),
                    last_error_code=code,
                    last_error_message=safe_error,
                )
                self._notify_job(
                    job,
                    "progress",
                    str(job["telegram_chat_id"]),
                    f"⚠️ {message}\n系统将在 1 分钟后自动重试（{retry_count + 1}/{self.settings.max_retries}）。",
                )
            else:
                self.sql.transition(job_id, JobState.FAILED, processing_finished_at=utc_now(), last_error_code=code, last_error_message=safe_error)
                self._notify_job(job, "failed", str(job["telegram_chat_id"]), message)

    def _classify_platform_error(
        self,
        platform: str,
        exc: Exception,
    ):
        if (
            platform == Platform.BILIBILI.value
            and self.settings.bilibili_session_enabled
        ):
            from backend.bilibili_session.recovery import classify_session_error

            return classify_session_error(self.settings, exc)
        return classify_platform_error(exc)

    def _defer_individual_platform_auth(
        self,
        job: dict,
        exc: Exception,
    ) -> bool:
        platform = str(job.get("source_platform") or "")
        if not platform and job.get("input_kind") == "url":
            try:
                platform = classify_url(str(job["input_value"])).platform.value
            except (KeyError, TypeError, ValueError):
                platform = ""
        if platform == Platform.XIAOHONGSHU.value:
            enabled = self.settings.xiaohongshu_session_enabled
            label = "小红书"
        elif platform == Platform.BILIBILI.value:
            enabled = self.settings.bilibili_session_enabled
            label = "哔哩哔哩"
        else:
            return False
        if not enabled:
            return False
        stage = str(job.get("state") or "")
        if stage not in {
            JobState.RECEIVED.value,
            JobState.EXTRACTING_SUBTITLES.value,
            JobState.DOWNLOADING.value,
            JobState.TRANSCRIBING.value,
        }:
            return False
        classified = self._classify_platform_error(platform, exc)
        if classified.code.value != "AUTH_EXPIRED":
            return False
        self.sql.record_platform_failure(
            platform,
            classified.code.value,
            immediate_open=True,
            rate_limited=False,
        )
        self.sql.transition(
            str(job["id"]),
            JobState.RECEIVED,
            source_platform=platform,
            next_attempt_at=datetime.now(timezone.utc) + timedelta(days=3650),
            processing_started_at=None,
            last_error_code=classified.code.value,
            last_error_message=classified.safe_message,
        )
        if platform == Platform.XIAOHONGSHU.value:
            from backend.xhs_session.recovery import trigger_session_refresh
        else:
            from backend.bilibili_session.recovery import trigger_session_refresh

        triggered = trigger_session_refresh(self.settings)
        self._notify_job(
            job,
            "progress",
            str(job["telegram_chat_id"]),
            (
                f"🔐 {label}登录状态已失效，当前录入已安全暂停。"
                "登录二维码正在发送；扫码成功后会自动继续。"
                if triggered
                else
                f"🔐 {label}登录状态已失效，当前录入已安全暂停。"
                "自动登录服务暂时未能启动，请联系管理员。"
            ),
        )
        return True

    def _defer_individual_xiaohongshu_auth(
        self,
        job: dict,
        exc: Exception,
    ) -> bool:
        """Compatibility wrapper for existing focused tests and callers."""
        return self._defer_individual_platform_auth(job, exc)

    def _handle_batch_failure(
        self,
        job: dict,
        exc: Exception,
        fallback_code: str,
        retryable: bool,
    ) -> None:
        job_id = str(job["id"])
        stage = str(job.get("state") or "")
        platform = str(job.get("source_platform") or "")
        if platform in {"youtube", "bilibili", "xiaohongshu"} and stage in {
            "RECEIVED",
            "EXTRACTING_SUBTITLES",
            "DOWNLOADING",
            "TRANSCRIBING",
        }:
            classified = self._classify_platform_error(platform, exc)
            if classified.code.value != "DISCOVERY_FAILED":
                gate = self.sql.record_platform_failure(
                    platform,
                    classified.code.value,
                    immediate_open=classified.immediate_open,
                    rate_limited=classified.rate_limited,
                )
                if gate["circuit_state"] == "OPEN":
                    state = (
                        BatchState.PAUSED_AUTH
                        if classified.code.value == "AUTH_EXPIRED"
                        else BatchState.PAUSED_RATE_LIMIT
                    )
                    retry_at = gate["blocked_until"] or (
                        datetime.now(timezone.utc) + timedelta(days=3650)
                    )
                    self.sql.pause_and_requeue_batch_child(
                        job_id,
                        state=state,
                        error_code=classified.code.value,
                        error_message=classified.safe_message,
                        next_attempt_at=retry_at,
                    )
                    if classified.code.value == "AUTH_EXPIRED":
                        if platform == Platform.XIAOHONGSHU.value:
                            from backend.xhs_session.recovery import (
                                trigger_session_refresh,
                            )
                        elif platform == Platform.BILIBILI.value:
                            from backend.bilibili_session.recovery import (
                                trigger_session_refresh,
                            )
                        else:
                            trigger_session_refresh = None
                        if trigger_session_refresh is not None:
                            trigger_session_refresh(self.settings)
                    return
                fallback_code = classified.code.value
                fallback_message = classified.safe_message
            else:
                fallback_message = redact_sensitive_text(
                    exc, max_length=1000
                )
        else:
            fallback_message = redact_sensitive_text(exc, max_length=1000)
        if retryable:
            self.sql.retry_batch_child(
                job_id,
                fallback_code,
                fallback_message,
                datetime.now(timezone.utc)
                + timedelta(seconds=self.settings.job_retry_delay_seconds),
            )
        else:
            self.sql.fail_batch_child(
                job_id,
                fallback_code,
                fallback_message,
            )

    async def _process_url(self, job: dict, job_dir: Path) -> None:
        job_id = str(job["id"]); url = validate_public_url(job["input_value"])
        chat_id = str(job["telegram_chat_id"])
        destination = job.get("selected_destination")
        self._ensure_batch_platform_slot(job)
        if (
            job.get("notification_mode") == "BATCH_SILENT"
            and destination
            and job.get("source_platform")
            and job.get("source_id")
            and job.get("canonical_url")
        ):
            document = self.sql.find_completed_document_for_destination(
                platform=job["source_platform"],
                source_id=job["source_id"],
                canonical_url=job["canonical_url"],
                destination=destination,
            )
            if document:
                self.sql.skip_batch_child_existing(
                    str(job["id"]), document["record_id"]
                )
                return
        self._ensure_batch_can_continue(job)
        self._notify_job(job, "progress", chat_id, "🔎 正在读取视频信息并检查平台字幕…")
        self.sql.transition(job_id, JobState.EXTRACTING_SUBTITLES)
        batch_context = (
            self.sql.batch_context_for_job(str(job["id"]))
            if job.get("notification_mode") == "BATCH_SILENT"
            and job.get("source_platform") == "xiaohongshu"
            else None
        )
        initial_metadata = job.get("metadata") or {}
        author_id = (
            str(batch_context["author_id"])
            if batch_context and batch_context.get("author_id")
            else str(initial_metadata["xiaohongshu_author_id"])
            if initial_metadata.get("xiaohongshu_author_id")
            else None
        )
        metadata, extraction = await self.metadata.from_url(
            url,
            author_id=author_id,
            # An "all works" discovery may legitimately contain items older
            # than the configured recent-preview window. Resolve batch items
            # through the complete paginated feed so positions after 50 do not
            # become false "not found" failures.
            author_scan_limit=0 if batch_context else None,
        )
        if (
            metadata.duration_seconds
            and metadata.duration_seconds > self.settings.max_video_duration_seconds
        ):
            raise ValueError("视频时长超过配置上限")
        if (
            job.get("notification_mode") == "BATCH_SILENT"
            and not self.sql.record_batch_child_duration(
                str(job["id"]), metadata.duration_seconds
            )
        ):
            raise BatchDeferred
        duplicate = self.sql.find_duplicate(canonical_url=metadata.canonical_url, platform=metadata.platform.value, source_id=metadata.source_id, media_sha256=None, transcript_checksum=None)
        if (
            can_reuse_duplicate_markdown(metadata.platform)
            and duplicate
            and duplicate.get("reusable_path")
            and Path(duplicate["reusable_path"]).exists()
        ):
            self._notify_job(job, "progress", chat_id, "♻️ 已发现相同视频，正在复用已有转录结果…")
            await self._reuse_duplicate(job, metadata, duplicate)
            return
        cover_path = None
        if should_store_video_cover(job, metadata.platform):
            cover_path = await download_xiaohongshu_cover(
                metadata.thumbnail_url,
                job_dir,
            )
        if metadata.platform == Platform.XIAOHONGSHU:
            # Signed CDN capabilities are worker-only. The durable Markdown
            # stores a local asset only for the cooking destination.
            metadata.thumbnail_url = None
        transient_media_url = extraction.get("_transient_media_url")
        subtitle = None
        language = None
        if not transient_media_url:
            subtitle, _, language = (
                await self.transcripts.video_processor.fetch_subtitles(
                    url, job_dir
                )
            )
        if subtitle:
            transcript = subtitle
            self._notify_job(job, "progress", chat_id, "✅ 已获取平台字幕\n正在整理转录文本…")
        else:
            self._ensure_batch_can_continue(job)
            self._ensure_batch_disk(job)
            self._notify_job(job, "progress", chat_id, "⬇️ 未发现可用字幕，开始下载音频…")
            self.sql.transition(job_id, JobState.DOWNLOADING)
            audio, _ = (
                await self.transcripts.video_processor.download_and_convert(
                    transient_media_url or url,
                    job_dir,
                    metadata.original_title,
                    protected_url=bool(transient_media_url),
                )
            )
            self._ensure_batch_can_continue(job)
            self._notify_job(job, "progress", chat_id, "✅ 音频下载完成\n🎙️ 开始使用 Whisper 转写…")
            self.sql.transition(job_id, JobState.TRANSCRIBING)
            transcript = await self.transcripts.local_transcript(Path(audio))
            self._ensure_batch_can_continue(job)
            self._notify_job(job, "progress", chat_id, "✅ 语音转写完成")
            language = None
        if (
            job.get("notification_mode") == "BATCH_SILENT"
            and job.get("source_platform") in {
                "youtube",
                "bilibili",
                "xiaohongshu",
            }
        ):
            self.sql.record_platform_success(job["source_platform"])
        metadata.language = language or metadata.language or "und"
        self._ensure_batch_can_continue(job)
        await self._stage(
            job,
            job_dir,
            metadata,
            transcript,
            None,
            cover_path=cover_path,
        )

    async def _process_media(self, job: dict, job_dir: Path) -> None:
        job_id = str(job["id"])
        chat_id = str(job["telegram_chat_id"])
        self._notify_job(job, "progress", chat_id, "⬇️ 正在接收并检查媒体文件…")
        self.sql.transition(job_id, JobState.DOWNLOADING)
        source = self._resolve_media_path(job["input_value"])
        if source.stat().st_size > self.settings.max_file_size_mb * 1024 * 1024:
            raise ValueError("file size exceeds limit")
        local = job_dir / ("source" + source.suffix.lower()[:12])
        shutil.copy2(source, local)
        media_hash = sha256_file(local)
        duration = probe_duration(local)
        if duration and duration > self.settings.max_video_duration_seconds:
            raise ValueError("视频时长超过配置上限")
        duplicate = self.sql.find_duplicate(canonical_url=None, platform="telegram", source_id=None, media_sha256=media_hash, transcript_checksum=None)
        metadata = SourceMetadata(platform=Platform.TELEGRAM, source_id=media_hash, original_title=job.get("caption") or source.stem, duration_seconds=duration, language="und")
        if duplicate and duplicate.get("reusable_path") and Path(duplicate["reusable_path"]).exists():
            self._notify_job(job, "progress", chat_id, "♻️ 已发现相同媒体，正在复用已有转录结果…")
            await self._reuse_duplicate(job, metadata, duplicate)
            self.cleanup.cleanup_media(job_dir)
            inbound_root = (self.settings.openclaw_media_root / "inbound").resolve()
            if source.is_relative_to(inbound_root):
                source.unlink(missing_ok=True)
            return
        self.sql.transition(job_id, JobState.TRANSCRIBING, media_sha256=media_hash)
        self._notify_job(job, "progress", chat_id, "✅ 媒体文件准备完成\n🎙️ 开始使用 Whisper 转写…")
        transcript = await self.transcripts.local_transcript(local)
        self._ensure_batch_can_continue(job)
        self._notify_job(job, "progress", chat_id, "✅ 语音转写完成")
        await self._stage(job, job_dir, metadata, transcript, media_hash)
        inbound_root = (self.settings.openclaw_media_root / "inbound").resolve()
        if source.is_relative_to(inbound_root):
            source.unlink(missing_ok=True)

    def _resolve_media_path(self, value: str) -> Path:
        if value.startswith("media://inbound/"):
            value = str(
                self.settings.openclaw_media_root
                / "inbound"
                / value.rsplit("/", 1)[-1]
            )
        path = Path(value).expanduser().resolve(strict=True)
        allowed = [
            self.settings.openclaw_media_root,
            self.settings.test_fixture_root,
        ]
        if not any(path.is_relative_to(root.resolve()) for root in allowed):
            raise ValueError("媒体文件不在允许目录")
        return path

    def _ensure_batch_can_continue(self, job: dict) -> None:
        if job.get("notification_mode") != "BATCH_SILENT":
            current = self.sql.get_job(str(job["id"]))
            if current and current["state"] in {
                JobState.PAUSED_USER.value,
                JobState.CANCELLED.value,
            }:
                raise JobControlRequested(current["state"])
            return
        context = self.sql.batch_context_for_job(str(job["id"]))
        if context and context["state"] == "CANCELLING":
            self.sql.cancel_batch_child(str(job["id"]))
            raise BatchDeferred
        if context and context["state"] not in {"QUEUED", "RUNNING"}:
            if str(context["state"]).startswith("PAUSED_"):
                self.sql.retry_batch_child(
                    str(job["id"]),
                    context.get("pause_code") or "BATCH_PAUSED",
                    context.get("pause_message") or "Batch is paused",
                    context.get("resume_not_before") or utc_now(),
                )
            else:
                self.sql.cancel_batch_child(
                    str(job["id"]),
                    "BATCH_NOT_RUNNABLE",
                    "Batch is no longer runnable",
                )
            raise BatchDeferred

    def _ensure_batch_platform_slot(self, job: dict) -> None:
        if job.get("notification_mode") != "BATCH_SILENT":
            return
        platform = str(job.get("source_platform") or "")
        if platform not in {"youtube", "bilibili", "xiaohongshu"}:
            return
        lease = self.sql.acquire_platform_request(platform)
        if lease.allowed:
            return
        retry_at = lease.retry_at or (
            datetime.now(timezone.utc) + timedelta(days=3650)
        )
        if lease.reason == "REQUEST_INTERVAL":
            self.sql.retry_batch_child(
                str(job["id"]),
                lease.reason,
                "Platform request interval has not elapsed",
                retry_at,
                increment_retry=False,
            )
        else:
            state = (
                BatchState.PAUSED_AUTH
                if lease.reason == "AUTH_EXPIRED"
                else BatchState.PAUSED_RATE_LIMIT
            )
            self.sql.pause_and_requeue_batch_child(
                str(job["id"]),
                state=state,
                error_code=lease.reason or "CIRCUIT_OPEN",
                error_message="平台访问已暂停，等待安全探测",
                next_attempt_at=retry_at,
            )
            if lease.reason == "AUTH_EXPIRED":
                if platform == Platform.XIAOHONGSHU.value:
                    from backend.xhs_session.recovery import trigger_session_refresh
                elif platform == Platform.BILIBILI.value:
                    from backend.bilibili_session.recovery import (
                        trigger_session_refresh,
                    )
                else:
                    trigger_session_refresh = None
                if trigger_session_refresh is not None:
                    trigger_session_refresh(self.settings)
        raise BatchDeferred

    def _ensure_batch_disk(self, job: dict) -> None:
        if job.get("notification_mode") != "BATCH_SILENT":
            return
        free_gb = shutil.disk_usage(self.settings.temp_root).free / 2**30
        if free_gb < self.settings.min_disk_free_gb:
            self.sql.pause_and_requeue_batch_child(
                str(job["id"]),
                state=BatchState.PAUSED_RESOURCE,
                error_code="LOW_DISK",
                error_message="可用磁盘空间不足",
                next_attempt_at=datetime.now(timezone.utc) + timedelta(minutes=15),
            )
            raise BatchDeferred

    async def _stage(
        self,
        job: dict,
        job_dir: Path,
        metadata: SourceMetadata,
        transcript: str,
        media_hash: str | None,
        *,
        cover_path: Path | None = None,
    ) -> None:
        job_id = str(job["id"])
        chat_id = str(job["telegram_chat_id"])
        self._notify_job(job, "progress", chat_id, "📝 正在生成摘要、标签和 Markdown…")
        self.sql.transition(job_id, JobState.BUILDING_MARKDOWN)
        enrichment = await self.enricher.enrich(metadata, transcript)
        document_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"video:{metadata.platform.value}:{metadata.source_id or job_id}"))
        cover_filename = (
            f"{safe_filename(enrichment.normalized_title)}"
            f"--{document_id[:8]}-cover.jpg"
            if cover_path
            else None
        )
        content, transcript_checksum = self.builder.build(
            document_id=document_id,
            metadata=metadata,
            enrichment=enrichment,
            transcript=transcript,
            telegram_chat_id=str(job["telegram_chat_id"]),
            telegram_message_id=str(job["telegram_message_id"]),
            cover_filename=cover_filename,
        )
        staging = self.builder.staging_path(self.settings.staging_root, job_id, enrichment.normalized_title)
        self.builder.atomic_write(staging, content)
        if not staging.exists() or staging.stat().st_size == 0:
            raise RuntimeError("staging markdown verification failed")
        self._notify_job(job, "progress", chat_id, "✅ Markdown 已生成并通过完整性检查")
        staging_checksum = sha256_file(staging)
        payload = {
            "source": metadata.model_dump(mode="json"),
            "enrichment": enrichment.model_dump(mode="json"),
            "domain": infer_domain(metadata, enrichment),
            "assets": {
                "cover_staging_path": str(cover_path),
                "cover_filename": cover_filename,
            } if cover_path and cover_filename else {},
        }
        self.sql.transition(job_id, JobState.ENRICHING_METADATA, metadata=payload, transcript_checksum=transcript_checksum, media_sha256=media_hash, staging_path=str(staging), staging_checksum=staging_checksum, document_id=document_id, source_platform=metadata.platform.value, source_id=metadata.source_id, canonical_url=metadata.canonical_url, title=enrichment.normalized_title, author=metadata.author, language=metadata.language, duration_seconds=metadata.duration_seconds)
        self.cleanup.cleanup_media(
            job_dir,
            keep={cover_path} if cover_path else None,
        )
        if job.get("destination_locked"):
            self.sql.transition(job_id, JobState.PERSISTING, next_attempt_at=utc_now())
            if requires_batch_persistence_requeue(job):
                self.sql.queue_batch_child_persistence(job_id)
        else:
            self.sql.transition(job_id, JobState.AWAITING_DESTINATION)
            self._notify_job(job, "awaiting", str(job["telegram_chat_id"]), job_id, enrichment.normalized_title, metadata.platform.value, metadata.author)

    async def _reuse_duplicate(self, job: dict, metadata: SourceMetadata, duplicate: dict) -> None:
        source = Path(duplicate["reusable_path"])
        target = self.settings.staging_root / f"duplicate--{str(job['id'])[:8]}.md"
        shutil.copy2(source, target)
        copied = dict(duplicate.get("metadata") or {})
        next_state = (
            JobState.PERSISTING if job.get("destination_locked")
            else JobState.AWAITING_DESTINATION
        )
        self.sql.transition(str(job["id"]), next_state, metadata=copied, staging_path=str(target), staging_checksum=sha256_file(target), transcript_checksum=duplicate.get("transcript_checksum"), media_sha256=duplicate.get("media_sha256"), document_id=duplicate.get("document_id"), source_platform=metadata.platform.value, source_id=metadata.source_id, canonical_url=metadata.canonical_url, title=duplicate.get("title"), author=duplicate.get("author"), language=duplicate.get("language"), duration_seconds=duplicate.get("duration_seconds"), **({"next_attempt_at": utc_now()} if job.get("destination_locked") else {}))
        if job.get("destination_locked"):
            if requires_batch_persistence_requeue(job):
                self.sql.queue_batch_child_persistence(str(job["id"]))
        else:
            self._notify_job(job, "awaiting", str(job["telegram_chat_id"]), str(job["id"]), duplicate.get("title") or "视频转录", metadata.platform.value, duplicate.get("author"))

    def _persist(self, job: dict) -> None:
        job_id = str(job["id"]); destination = Destination(job["selected_destination"])
        chat_id = str(job["telegram_chat_id"])
        destination_name = destination_label(destination)
        staging = Path(job["staging_path"])
        target = self.destinations.target_path(
            destination,
            job["title"],
            str(job["document_id"]),
            source_markdown=staging,
            metadata=job.get("metadata"),
        )
        self._ensure_batch_can_continue(job)
        self._notify_job(job, "progress", chat_id, f"📄 正在将 Markdown 写入{destination_name}…")
        if not staging.exists():
            if not target.exists(): raise RuntimeError("staging markdown is missing")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                os.replace(staging, target)
            else:
                staging.unlink()
        persisted_checksum = sha256_file(target)
        try:
            assets = (job.get("metadata") or {}).get("assets") or {}
            cover_filename = assets.get("cover_filename")
            cover_staging_path = assets.get("cover_staging_path")
            if cover_filename and cover_staging_path:
                asset_dir = target.parent / "Assets"
                asset_dir.mkdir(parents=True, exist_ok=True)
                cover_target = asset_dir / Path(cover_filename).name
                cover_source = Path(cover_staging_path)
                if cover_source.is_file() and not cover_target.exists():
                    os.replace(cover_source, cover_target)
                elif cover_source.is_file():
                    cover_source.unlink()
                if not cover_target.is_file():
                    raise RuntimeError("cover asset is missing")
                try:
                    cover_source.parent.rmdir()
                except OSError:
                    pass
            self._notify_job(job, "progress", chat_id, "🧩 Markdown 已写入，正在 chunking 并生成 embeddings…")
            self.destinations.vectors.ingest(destination, target)
            self._notify_job(job, "progress", chat_id, "✅ Chunking 和 embeddings 已完成\n正在登记 SQL 和 pgvector 状态…")
            document_id = self.sql.upsert_document(
                job, target, persisted_checksum, "completed"
            )
            if job.get("notification_mode") == "BATCH_SILENT":
                self.sql.complete_batch_child(job_id, document_id)
            else:
                self.sql.transition(job_id, JobState.COMPLETED, document_id=document_id, processing_finished_at=utc_now())
                self._notify_job(job, "completed", str(job["telegram_chat_id"]), destination.value, job["title"])
        except Exception:  # noqa: BLE001
            try:
                self.sql.upsert_document(
                    job, target, persisted_checksum, "partial"
                )
            finally: raise

    def _cleanup_cancelled_artifacts(self, job_id: str) -> None:
        job = self.sql.get_job(job_id)
        if not job:
            return
        job_dir = (self.settings.temp_root / job_id).resolve()
        temp_root = self.settings.temp_root.resolve()
        if job_dir.is_relative_to(temp_root) and job_dir.is_dir():
            shutil.rmtree(job_dir)
        staging_value = job.get("staging_path")
        if staging_value:
            staging = Path(staging_value).resolve()
            staging_root = self.settings.staging_root.resolve()
            if staging.is_relative_to(staging_root) and staging.is_file():
                staging.unlink()
