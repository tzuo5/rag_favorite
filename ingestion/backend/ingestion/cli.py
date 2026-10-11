from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import shutil
import socket
import tempfile
from pathlib import Path

from .batch_notifications import BatchNotificationDispatcher
from .batch_presenter import batch_text
from .batch_repository import (
    ActiveBatchExistsError,
    BatchBudgetExceededError,
    BatchForbiddenError,
    BatchNotFoundError,
    BatchStateError,
)
from .batch_service import (
    BatchConfirmationDisabledError,
    BatchConfirmationService,
    BatchPlatformUnavailableError,
    BatchResourceError,
)
from .config import Settings
from .discovery.errors import classify_platform_error
from .discovery.models import DiscoveryAdapterError
from .discovery.registry import (
    AuthorDiscoveryAdapterRegistry,
    author_discovery_enabled,
    author_scan_limit,
    capability_payload,
)
from .discovery.url_classifier import (
    ShortLinkResolutionError,
    classify_input_url,
)
from .discovery.xiaohongshu import (
    XiaohongshuAuthorDiscoveryAdapter,
    cookie_file_diagnostics,
)
from .discovery_worker import AuthorDiscoveryWorker
from .logging import configure_logging
from .media import probe_duration
from .models import (
    SELECTABLE_DESTINATIONS,
    AccessProbeStatus,
    Destination,
    DiscoveryEligibility,
    Platform,
    UrlKind,
    destination_label,
)
from .notifier import TelegramNotifier
from .observation import collect_service_observation
from .repository import SqlRepository
from .security import extract_urls, redact_sensitive_text, validate_public_url
from .service import VideoIngestionService
from .work_presenter import (
    TERMINAL_BATCH_STATES,
    TERMINAL_DISCOVERY_STATES,
    TERMINAL_JOB_STATES,
    present_work,
)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("--user-id", required=True); enqueue.add_argument("--chat-id", required=True); enqueue.add_argument("--message-id", required=True)
    enqueue.add_argument("--text", default=""); enqueue.add_argument("--media-path"); enqueue.add_argument("--media-type"); enqueue.add_argument("--caption")
    telegram_notify = commands.add_parser("telegram-notify")
    telegram_notify.add_argument("--user-id", required=True)
    telegram_notify.add_argument("--chat-id", required=True)
    telegram_notify.add_argument("--text", required=True)
    start_prompt = commands.add_parser("start-prompt")
    start_prompt.add_argument("--user-id", required=True)
    start_prompt.add_argument("--chat-id", required=True)
    start_prompt.add_argument("--request-id", required=True)
    destination_prompt = commands.add_parser("destination-prompt")
    destination_prompt.add_argument("--user-id", required=True)
    destination_prompt.add_argument("--chat-id", required=True)
    destination_prompt.add_argument("--job-id", required=True)
    destination_prompt.add_argument("--platform", required=True)
    choose = commands.add_parser("choose")
    choose.add_argument("--job-id", required=True); choose.add_argument("--user-id", required=True); choose.add_argument(
        "--destination",
        choices=[*(item.value for item in SELECTABLE_DESTINATIONS), "cancel"],
        required=True,
    )
    status = commands.add_parser("status")
    status.add_argument("--user-id", required=True)
    work_status = commands.add_parser("work-status")
    work_status.add_argument("--user-id", required=True)
    work_status.add_argument(
        "--kind", choices=["job", "discovery", "batch"]
    )
    work_status.add_argument("--object-id")
    for command in ("work-pause", "work-resume", "work-cancel"):
        control = commands.add_parser(command)
        control.add_argument("--user-id", required=True)
        control.add_argument(
            "--kind", choices=["job", "discovery", "batch"]
        )
        control.add_argument("--object-id")
    classify = commands.add_parser("classify")
    classify.add_argument("--url", required=True)
    discover = commands.add_parser("discover")
    discover.add_argument("--user-id", required=True)
    discover.add_argument("--chat-id", required=True)
    discover.add_argument("--message-id", required=True)
    discover.add_argument("--url", required=True)
    discovery_status = commands.add_parser("discovery-status")
    discovery_status.add_argument("--discovery-id", required=True)
    discovery_status.add_argument("--user-id", required=True)
    discovery_prompt = commands.add_parser("discovery-prompt")
    discovery_prompt.add_argument("--discovery-id", required=True)
    discovery_prompt.add_argument("--user-id", required=True)
    discovery_prompt.add_argument("--chat-id", required=True)
    confirm = commands.add_parser("confirm-batch")
    confirm.add_argument("--discovery-id", required=True)
    confirm.add_argument("--user-id", required=True)
    confirm.add_argument(
        "--destination",
        choices=[item.value for item in SELECTABLE_DESTINATIONS],
        required=True,
    )
    confirm.add_argument("--limit", type=int, default=10)
    confirm.add_argument(
        "--selection-kind",
        choices=["latest", "all_preview"],
        default="latest",
    )
    preview = commands.add_parser("batch-preview")
    preview.add_argument("--discovery-id", required=True)
    preview.add_argument("--user-id", required=True)
    preview.add_argument("--limit", type=int, required=True)
    preview.add_argument(
        "--selection-kind",
        choices=["latest", "all_preview"],
        default="latest",
    )
    batch_status = commands.add_parser("batch-status")
    batch_status.add_argument("--user-id", required=True)
    batch_status.add_argument("--batch-id")
    for command in ("batch-pause", "batch-resume", "batch-cancel"):
        control = commands.add_parser(command)
        control.add_argument("--user-id", required=True)
        control.add_argument("--batch-id")
    probe = commands.add_parser("probe")
    probe.add_argument("--url", required=True)
    acceptance = commands.add_parser("author-acceptance")
    acceptance.add_argument("--url", required=True)
    acceptance.add_argument("--limit", type=int, default=1)
    acceptance.add_argument("--resolve-first-video", action="store_true")
    acceptance.add_argument("--download-first-video", action="store_true")
    commands.add_parser("expire-discoveries")
    commands.add_parser("discovery-worker")
    commands.add_parser("batch-notification-worker")
    commands.add_parser("batch-metrics")
    commands.add_parser("batch-audit")
    observation = commands.add_parser("release-observation")
    observation.add_argument("--hours", type=int, default=24)
    commands.add_parser("security-audit")
    commands.add_parser("capabilities")
    session_login = commands.add_parser("session-login")
    session_login.add_argument(
        "--platform", choices=["xiaohongshu", "bilibili"], required=True
    )
    commands.add_parser("pause-all-batches")
    commands.add_parser("worker"); commands.add_parser("health"); commands.add_parser("cleanup")
    return root


def deliver_telegram_notification(
    *,
    user_id: str,
    chat_id: str,
    text: str,
    settings: Settings,
    notifier: TelegramNotifier | None = None,
) -> dict:
    if settings.allowed_user_ids and user_id not in settings.allowed_user_ids:
        return {"ok": False, "status": "forbidden"}
    notifier = notifier or TelegramNotifier()
    if not notifier.token:
        return {"ok": False, "status": "not_configured"}
    notifier.progress(chat_id, text)
    return {"ok": True, "status": "delivered"}


def deliver_start_prompt(
    *,
    user_id: str,
    chat_id: str,
    request_id: str,
    settings: Settings,
    notifier: TelegramNotifier | None = None,
) -> dict:
    if settings.allowed_user_ids and user_id not in settings.allowed_user_ids:
        return {"ok": False, "status": "forbidden"}
    if not re.fullmatch(r"[0-9a-f-]{36}", request_id, re.IGNORECASE):
        return {"ok": False, "status": "invalid_request"}
    notifier = notifier or TelegramNotifier()
    if not notifier.token:
        return {"ok": False, "status": "not_configured"}
    notifier.start_confirmation(chat_id, request_id)
    return {"ok": True, "status": "delivered"}


def deliver_destination_prompt(
    *,
    user_id: str,
    chat_id: str,
    job_id: str,
    platform: str,
    settings: Settings,
    repository: SqlRepository,
    notifier: TelegramNotifier | None = None,
) -> dict:
    if settings.allowed_user_ids and user_id not in settings.allowed_user_ids:
        return {"ok": False, "status": "forbidden"}
    job = repository.get_job(job_id)
    if (
        not job
        or str(job["telegram_user_id"]) != str(user_id)
        or str(job["telegram_chat_id"]) != str(chat_id)
        or job["state"] != "AWAITING_DESTINATION"
    ):
        return {"ok": False, "status": "not_ready"}
    notifier = notifier or TelegramNotifier()
    if not notifier.token:
        return {"ok": False, "status": "not_configured"}
    notifier.awaiting_link(chat_id, job_id, platform)
    return {"ok": True, "status": "delivered"}


async def worker() -> None:
    service = VideoIngestionService()
    # A fresh worker cannot have a live predecessor for these processing
    # states: the systemd unit is single-instance. Recover them immediately
    # instead of leaving a just-orphaned job stuck until the normal timeout.
    await asyncio.to_thread(service.sql.recover_stale, 0)
    await asyncio.to_thread(service.sql.reconcile_batches)
    while True:
        job = await asyncio.to_thread(service.sql.claim_next)
        if job:
            try: await asyncio.wait_for(service.process(job), timeout=service.settings.job_timeout_seconds)
            except asyncio.TimeoutError:
                models = __import__("backend.ingestion.models", fromlist=["JobState", "utc_now"])
                current = service.sql.get_job(str(job["id"])) or job
                if current.get("state") in {
                    "PAUSED_USER", "CANCELLED"
                }:
                    continue
                if job.get("notification_mode") == "BATCH_SILENT":
                    service.sql.fail_batch_child(
                        str(job["id"]), "TIMEOUT", "job timeout"
                    )
                else:
                    service.sql.transition(str(job["id"]), models.JobState.FAILED, processing_finished_at=models.utc_now(), last_error_code="TIMEOUT", last_error_message="job timeout")
            continue
        await asyncio.sleep(2)


async def discovery_worker() -> None:
    from datetime import datetime, timezone

    settings = Settings()
    repository = SqlRepository(settings)
    adapters = AuthorDiscoveryAdapterRegistry(settings)
    runner = AuthorDiscoveryWorker(repository, adapters, TelegramNotifier())
    await asyncio.to_thread(
        repository.recover_stale_author_discoveries, 0
    )
    while True:
        result = await runner.run_once()
        if result is None:
            await asyncio.sleep(2)
            continue
        if result.get("state") == "QUEUED":
            retry_at = result.get("retry_at")
            if retry_at:
                target = datetime.fromisoformat(retry_at)
                delay = (target - datetime.now(timezone.utc)).total_seconds()
                await asyncio.sleep(max(2, min(delay, 60)))
            else:
                await asyncio.sleep(60)


async def batch_notification_worker() -> None:
    repository = SqlRepository(Settings())
    dispatcher = BatchNotificationDispatcher(repository, TelegramNotifier())
    while True:
        try:
            delivered = await asyncio.to_thread(dispatcher.run_once)
        except Exception:  # noqa: BLE001 - worker loop must survive notifier faults
            logging = __import__("logging")
            logging.getLogger(__name__).exception(
                "batch notification delivery failed"
            )
            await asyncio.sleep(2)
            continue
        if delivered is None:
            await asyncio.sleep(2)


async def probe_author(url: str, repository: SqlRepository) -> dict:
    try:
        classified = classify_input_url(url)
    except ShortLinkResolutionError as exc:
        return {
            "ok": False,
            "status": "short_link_resolution_failed",
            "platform": exc.platform.value,
        }
    if classified.kind != UrlKind.AUTHOR_PAGE or not classified.normalized_url:
        return {"ok": False, "status": "unsupported_url"}
    lease = repository.acquire_platform_request(classified.platform, probe=True)
    if not lease.allowed:
        return {
            "ok": False,
            "status": lease.reason,
            "retry_at": lease.retry_at.isoformat() if lease.retry_at else None,
        }
    adapter = AuthorDiscoveryAdapterRegistry(
        repository.settings
    ).for_platform(classified.platform)
    result = await adapter.probe_access(classified.normalized_url)
    if result.status == AccessProbeStatus.OK:
        repository.record_platform_success(classified.platform)
    else:
        repository.record_platform_failure(
            classified.platform,
            result.error_code or result.status.value,
            immediate_open=result.status in {
                AccessProbeStatus.AUTH_REQUIRED,
                AccessProbeStatus.BLOCKED,
            },
            rate_limited=result.status == AccessProbeStatus.RATE_LIMITED,
        )
    return {
        "ok": result.status == AccessProbeStatus.OK,
        "status": result.status.value,
        "error_code": result.error_code,
        "platform": classified.platform.value,
    }


async def author_acceptance(
    url: str,
    repository: SqlRepository,
    *,
    limit: int,
    resolve_first_video: bool = False,
    download_first_video: bool = False,
) -> dict:
    """Run an operator canary without creating discovery or batch rows."""
    try:
        classified = classify_input_url(url)
    except ShortLinkResolutionError as exc:
        return {
            "ok": False,
            "status": "short_link_resolution_failed",
            "platform": exc.platform.value,
        }
    if classified.kind != UrlKind.AUTHOR_PAGE or not classified.normalized_url:
        return {"ok": False, "status": "unsupported_url"}
    platform = classified.platform
    maximum = repository.settings.author_discovery_scan_limit
    if not 1 <= limit <= maximum:
        return {
            "ok": False,
            "status": "invalid_limit",
            "platform": platform.value,
            "maximum": maximum,
        }
    if platform == Platform.XIAOHONGSHU:
        cookie_file = cookie_file_diagnostics(repository.settings)
        if cookie_file["auth_status"] != "ready":
            return {
                "ok": False,
                "status": "auth_not_ready",
                "platform": platform.value,
                "cookie_file": cookie_file,
            }
    if (resolve_first_video or download_first_video) and (
        platform != Platform.XIAOHONGSHU
    ):
        return {
            "ok": False,
            "status": "media_canary_unsupported",
            "platform": platform.value,
        }
    lease = repository.acquire_platform_request(platform, probe=True)
    if not lease.allowed:
        return {
            "ok": False,
            "status": lease.reason,
            "platform": platform.value,
            "retry_at": lease.retry_at.isoformat() if lease.retry_at else None,
        }
    adapter = AuthorDiscoveryAdapterRegistry(
        repository.settings
    ).for_platform(platform)
    try:
        discovery = await adapter.discover(
            classified.normalized_url,
            scan_limit=limit,
        )
        eligible = [
            item for item in discovery.works
            if item.eligibility == DiscoveryEligibility.ELIGIBLE
        ]
        result: dict = {
            "ok": True,
            "status": "discovery_ok",
            "platform": platform.value,
            "feature_flag_enabled": author_discovery_enabled(
                repository.settings, platform
            ),
            "persistence": {
                "discovery_rows_created": 0,
                "batch_rows_created": 0,
            },
            "author": {
                "id": discovery.author.author_id,
                "display_name": discovery.author.display_name,
            },
            "requested_limit": limit,
            "discovered_count": len(discovery.works),
            "eligible_count": len(eligible),
            "truncated": discovery.truncated,
            "extractor": discovery.extractor_name,
            "works": [
                {
                    "source_id": item.source_id,
                    "title": item.title,
                    "duration_seconds": item.duration_seconds,
                    "eligibility": item.eligibility.value,
                }
                for item in discovery.works
            ],
        }
        if resolve_first_video or download_first_video:
            if not eligible:
                raise DiscoveryAdapterError(
                    "DISCOVERY_FAILED",
                    "小红书作者最近作品中没有可执行的视频",
                )
            selected = eligible[0]
            media = await adapter.resolve_note_media(
                discovery.author.author_id,
                selected.source_id,
                scan_limit=limit,
            )
            result["media_canary"] = {
                "status": "resolved",
                "source_id": selected.source_id,
                "title": media.get("title"),
                "duration_seconds": media.get("duration"),
                "author_match": (
                    media.get("author_id") == discovery.author.author_id
                ),
            }
            if download_first_video:
                repository.settings.ensure_directories()
                free_gb = (
                    shutil.disk_usage(repository.settings.temp_root).free
                    / 2**30
                )
                if free_gb < repository.settings.min_disk_free_gb:
                    raise RuntimeError("可用磁盘空间不足")
                from backend.video_processor import VideoProcessor

                temporary_path: Path | None = None
                with tempfile.TemporaryDirectory(
                    prefix="xiaohongshu-canary-",
                    dir=repository.settings.temp_root,
                ) as temporary:
                    temporary_path = Path(temporary)
                    audio_path, _ = await VideoProcessor().download_and_convert(
                        media["media_url"],
                        temporary_path,
                        media.get("title"),
                        protected_url=True,
                    )
                    audio = Path(audio_path)
                    result["media_canary"].update({
                        "status": "downloaded",
                        "download_bytes": audio.stat().st_size,
                        "download_duration_seconds": probe_duration(audio),
                    })
                result["media_canary"]["temporary_files_removed"] = (
                    temporary_path is not None
                    and not temporary_path.exists()
                )
        repository.record_platform_success(platform)
        return result
    except DiscoveryAdapterError as exc:
        code = exc.code
        message = exc.safe_message
        immediate_open = code in {
            "AUTH_EXPIRED", "BOT_CHECK", "PLATFORM_BLOCKED",
        }
        rate_limited = code == "RATE_LIMITED"
    except Exception as exc:  # noqa: BLE001 - normalize all platform failures
        classified_error = classify_platform_error(exc)
        code = classified_error.code.value
        message = classified_error.safe_message
        immediate_open = classified_error.immediate_open
        rate_limited = classified_error.rate_limited
    repository.record_platform_failure(
        platform,
        code,
        immediate_open=immediate_open,
        rate_limited=rate_limited,
    )
    return {
        "ok": False,
        "status": "acceptance_failed",
        "platform": platform.value,
        "error_code": code,
        "message": message,
    }


def health() -> dict:
    settings = Settings()
    settings.ensure_directories()
    repo = SqlRepository(settings)
    result = {
        "postgres": False,
        "vector": False,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ytdlp": importlib.util.find_spec("yt_dlp") is not None,
        "youtube_pot_provider": not bool(settings.ytdlp_cookies_file),
        "directories": False,
        "disk_free_gb": round(
            shutil.disk_usage(settings.root).free / 2**30, 2
        ),
        "whisper": {
            "model": os.getenv("WHISPER_MODEL_SIZE", "medium"),
            "device": os.getenv("WHISPER_DEVICE", "cpu"),
            "compute_type": os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
        },
    }
    try:
        with repo.connection() as conn:
            conn.execute("SELECT 1").fetchone()
            result["postgres"] = True
            result["vector"] = bool(conn.execute(
                "SELECT 1 FROM pg_extension WHERE extname='vector'"
            ).fetchone())
    except Exception:  # noqa: BLE001 - health reports failure as data
        result["postgres"] = False
    if settings.ytdlp_cookies_file:
        try:
            with socket.create_connection(("127.0.0.1", 4416), timeout=2):
                result["youtube_pot_provider"] = True
        except OSError:
            result["youtube_pot_provider"] = False
    result["directories"] = all(
        path.is_dir() and os.access(path, os.W_OK)
        for path in (settings.temp_root, settings.staging_root)
    )
    required = (
        "postgres",
        "vector",
        "ffmpeg",
        "ytdlp",
        "youtube_pot_provider",
        "directories",
    )
    result["ok"] = (
        all(result[key] for key in required)
        and result["disk_free_gb"] >= settings.min_disk_free_gb
    )
    return result


def database_author_batch_capabilities(repository: SqlRepository) -> dict:
    try:
        with repository.connection() as conn:
            rows = conn.execute(
                """
                SELECT table_record.relname AS table_name,
                       constraint_record.conname AS constraint_name,
                       pg_get_constraintdef(constraint_record.oid) AS definition
                FROM pg_constraint constraint_record
                JOIN pg_class table_record
                  ON table_record.oid=constraint_record.conrelid
                WHERE table_record.relname IN (
                    'video_author_discoveries',
                    'video_author_discovery_items',
                    'video_ingestion_batches',
                    'video_ingestion_batch_items'
                )
                  AND constraint_record.contype='c'
                  AND (
                    constraint_record.conname LIKE '%platform_check'
                    OR constraint_record.conname =
                       'video_ingestion_batches_total_count_check'
                  )
                """
            ).fetchall()
    except Exception:  # noqa: BLE001 - capability degrades to unavailable
        return {
            "status": "unavailable",
            "platforms": [],
            "max_items": 0,
        }
    definitions = " ".join(str(row["definition"]) for row in rows)
    platforms = [
        platform.value
        for platform in (
            Platform.YOUTUBE,
            Platform.BILIBILI,
            Platform.XIAOHONGSHU,
        )
        if f"'{platform.value}'" in definitions
    ]
    total_definition = next(
        (
            str(row["definition"])
            for row in rows
            if row["constraint_name"]
            == "video_ingestion_batches_total_count_check"
        ),
        "",
    )
    limit_match = re.search(
        r"(?:BETWEEN\s+1\s+AND|<=)\s*(\d+)",
        total_definition,
        re.IGNORECASE,
    )
    return {
        "status": "ready" if rows else "missing",
        "platforms": platforms,
        "max_items": int(limit_match.group(1)) if limit_match else 0,
    }


def sensitive_persistence_audit(repository: SqlRepository) -> dict:
    """Count secret-like values in persistence without returning row data."""
    sources = {
        "video_ingestion_jobs": (
            "SELECT to_jsonb(job)::text AS payload "
            "FROM video_ingestion_jobs job "
            "WHERE source_platform='xiaohongshu' "
            "OR input_value ILIKE '%%xiaohongshu.com%%'"
        ),
        "video_knowledge_documents": (
            "SELECT to_jsonb(document)::text AS payload "
            "FROM video_knowledge_documents document "
            "WHERE source_platform='xiaohongshu'"
        ),
        "video_author_discoveries": (
            "SELECT to_jsonb(discovery)::text AS payload "
            "FROM video_author_discoveries discovery "
            "WHERE platform='xiaohongshu'"
        ),
        "video_author_discovery_items": (
            "SELECT to_jsonb(item)::text AS payload "
            "FROM video_author_discovery_items item "
            "WHERE platform='xiaohongshu'"
        ),
        "video_ingestion_batches": (
            "SELECT to_jsonb(batch)::text AS payload "
            "FROM video_ingestion_batches batch "
            "WHERE platform='xiaohongshu'"
        ),
        "video_ingestion_batch_items": (
            "SELECT to_jsonb(item)::text AS payload "
            "FROM video_ingestion_batch_items item "
            "WHERE platform='xiaohongshu'"
        ),
    }
    pattern = (
        r"xsec_token|web_session|"
        r"[?&](?:sign|signature)=|"
        r"[\"']a1[\"']\s*:|"
        r"[\"'](?:_transient_media_url|media_url)[\"']\s*:"
    )
    counts = {}
    try:
        with repository.connection() as conn:
            for table, source in sources.items():
                row = conn.execute(
                    f"""
                    SELECT count(*) AS count
                    FROM ({source}) candidate
                    WHERE candidate.payload ~* %s
                    """,
                    (pattern,),
                ).fetchone()
                counts[table] = int(row["count"])
    except Exception:  # noqa: BLE001 - audit returns a safe status only
        return {
            "ok": False,
            "status": "unavailable",
            "match_count": None,
            "tables": {},
        }
    total = sum(counts.values())
    return {
        "ok": total == 0,
        "status": "clean" if total == 0 else "matches_found",
        "match_count": total,
        "tables": counts,
    }


def _work_row(
    repository: SqlRepository,
    user_id: str,
    *,
    kind: str | None = None,
    object_id: str | None = None,
) -> tuple[str, dict] | None:
    if bool(kind) != bool(object_id):
        return None
    if kind == "job":
        row = repository.get_owned_job(str(object_id), user_id)
        return (kind, row) if row else None
    if kind == "discovery":
        row = repository.get_author_discovery(str(object_id), user_id)
        return (kind, row) if row else None
    if kind == "batch":
        row = repository.get_owned_batch(str(object_id), user_id)
        return (kind, row) if row else None

    candidates = [
        ("job", repository.latest_job(user_id)),
        ("discovery", repository.latest_author_discovery(user_id)),
        ("batch", repository.latest_batch(user_id)),
    ]
    active = {
        "job": lambda state: state not in TERMINAL_JOB_STATES,
        "discovery": lambda state: state not in TERMINAL_DISCOVERY_STATES,
        "batch": lambda state: state not in TERMINAL_BATCH_STATES,
    }
    available = [
        (candidate_kind, row)
        for candidate_kind, row in candidates
        if row is not None
    ]
    if not available:
        return None
    return max(
        available,
        key=lambda candidate: (
            active[candidate[0]](str(candidate[1]["state"])),
            candidate[1]["created_at"],
        ),
    )


def _cleanup_job_artifacts(settings: Settings, job: dict) -> None:
    job_dir = (settings.temp_root / str(job["id"])).resolve()
    temp_root = settings.temp_root.resolve()
    if job_dir.is_relative_to(temp_root) and job_dir.is_dir():
        shutil.rmtree(job_dir)
    staging_value = job.get("staging_path")
    if staging_value:
        staging = Path(staging_value).resolve()
        staging_root = settings.staging_root.resolve()
        if staging.is_relative_to(staging_root) and staging.is_file():
            staging.unlink()


def main() -> int:
    configure_logging(); args = parser().parse_args(); settings = Settings(); repo = SqlRepository(settings)
    if args.command == "telegram-notify":
        result = deliver_telegram_notification(
            user_id=args.user_id,
            chat_id=args.chat_id,
            text=args.text,
            settings=settings,
        )
        print(json.dumps(result))
        return 0 if result["ok"] else 3
    if args.command == "start-prompt":
        result = deliver_start_prompt(
            user_id=args.user_id,
            chat_id=args.chat_id,
            request_id=args.request_id,
            settings=settings,
        )
        print(json.dumps(result))
        return 0 if result["ok"] else 3
    if args.command == "destination-prompt":
        result = deliver_destination_prompt(
            user_id=args.user_id,
            chat_id=args.chat_id,
            job_id=args.job_id,
            platform=args.platform,
            settings=settings,
            repository=repo,
        )
        print(json.dumps(result))
        return 0 if result["ok"] else 3
    if args.command == "enqueue":
        if settings.allowed_user_ids and args.user_id not in settings.allowed_user_ids:
            print(json.dumps({"ok": False, "status": "forbidden"})); return 3
        rows = []
        destination_jobs = []
        discoveries = []
        safe_caption = (
            redact_sensitive_text(args.caption, max_length=1000)
            if args.caption
            else None
        )
        for url in extract_urls(args.text):
            validate_public_url(url)
            try:
                classified = classify_input_url(url)
            except ShortLinkResolutionError as exc:
                print(json.dumps({
                    "ok": False,
                    "status": "short_link_resolution_failed",
                    "platform": exc.platform.value,
                }))
                return 4
            if classified.kind == UrlKind.AUTHOR_PAGE:
                if not author_discovery_enabled(settings, classified.platform):
                    print(json.dumps({
                        "ok": False,
                        "status": "author_discovery_disabled",
                        "platform": classified.platform.value,
                    }))
                    return 4
                discoveries.append(repo.create_author_discovery(
                    user_id=args.user_id,
                    chat_id=args.chat_id,
                    message_id=args.message_id,
                    input_url=classified.normalized_url,
                    platform=classified.platform,
                    canonical_author_url=classified.normalized_url,
                    scan_limit=author_scan_limit(
                        settings, classified.platform
                    ),
                ))
            elif classified.kind == UrlKind.UNSUPPORTED_COLLECTION:
                print(json.dumps({
                    "ok": False,
                    "status": "unsupported_collection",
                }))
                return 4
            else:
                safe_url = classified.normalized_url or url
                initial_metadata = None
                initial_platform = None
                initial_source_id = None
                initial_canonical_url = None
                if (
                    classified.kind == UrlKind.SINGLE_WORK
                    and classified.platform == Platform.XIAOHONGSHU
                    and classified.transient_access_url
                    and classified.normalized_url
                ):
                    note_id = classified.normalized_url.rsplit("/", 1)[-1]
                    try:
                        resolved = asyncio.run(
                            XiaohongshuAuthorDiscoveryAdapter(
                                settings
                            ).resolve_note_access_url(
                                classified.transient_access_url,
                                note_id,
                            )
                        )
                    except DiscoveryAdapterError as exc:
                        if exc.code == "AUTH_EXPIRED":
                            from backend.xhs_session.recovery import (
                                trigger_session_refresh,
                            )

                            triggered = trigger_session_refresh(settings)
                            print(json.dumps({
                                "ok": False,
                                "status": "xiaohongshu_auth_required",
                                "platform": Platform.XIAOHONGSHU.value,
                                "login_triggered": triggered,
                            }))
                            return 4
                        print(json.dumps({
                            "ok": False,
                            "status": "xiaohongshu_share_context_failed",
                            "platform": Platform.XIAOHONGSHU.value,
                            "error_code": exc.code,
                        }))
                        return 4
                    except (ValueError, OSError):
                        print(json.dumps({
                            "ok": False,
                            "status": "xiaohongshu_share_context_failed",
                            "platform": Platform.XIAOHONGSHU.value,
                        }))
                        return 4
                    initial_metadata = {
                        "xiaohongshu_author_id": resolved["author_id"],
                    }
                    initial_platform = Platform.XIAOHONGSHU.value
                    initial_source_id = note_id
                    initial_canonical_url = classified.normalized_url
                row = repo.create_job(
                    user_id=args.user_id,
                    chat_id=args.chat_id,
                    message_id=args.message_id,
                    input_kind="url",
                    input_value=safe_url,
                    caption=safe_caption,
                    metadata=initial_metadata,
                    source_platform=initial_platform,
                    source_id=initial_source_id,
                    canonical_url=initial_canonical_url,
                    await_destination=True,
                )
                rows.append(row)
                destination_jobs.append({
                    "id": str(row["id"]),
                    "platform": classified.platform.value,
                })
        if args.media_path:
            rows.append(repo.create_job(user_id=args.user_id, chat_id=args.chat_id, message_id=args.message_id, input_kind="media", input_value=args.media_path, media_type=args.media_type, caption=safe_caption))
        print(json.dumps({
            "ok": bool(rows or discoveries),
            "jobs": [str(row["id"]) for row in rows],
            "destination_jobs": destination_jobs,
            "discoveries": [str(row["id"]) for row in discoveries],
        }))
        return 0 if rows or discoveries else 2
    if args.command == "choose":
        destination = None if args.destination == "cancel" else Destination(args.destination)
        print(json.dumps({"status": repo.select_destination(args.job_id, args.user_id, destination)})); return 0
    if args.command == "status":
        job = repo.latest_job(args.user_id)
        if not job:
            print(json.dumps({"status": "none", "text": "还没有视频录入任务。"}, ensure_ascii=False)); return 0
        state = job["state"]
        title = job.get("title") or "待识别视频"
        destination = destination_label(job.get("selected_destination"))
        if state == "COMPLETED": text = f"✅ 最近一次录入已成功\n知识库：{destination}\n标题：{title}"
        elif state == "AWAITING_DESTINATION": text = f"正在等待选择知识库。\n标题：{title}"
        elif state == "FAILED": text = f"最近一次录入失败。\n标题：{title}\n原因：{job.get('last_error_code') or '未知'}"
        elif state == "CANCELLED": text = f"最近一次录入已取消。\n标题：{title}"
        else: text = f"最近一次任务正在处理。\n阶段：{state}\n标题：{title}"
        print(json.dumps({"status": state, "text": text, "job_id": str(job["id"])}, ensure_ascii=False)); return 0
    if args.command == "work-status":
        selected = _work_row(
            repo,
            args.user_id,
            kind=args.kind,
            object_id=args.object_id,
        )
        if not selected:
            print(json.dumps({
                "ok": False,
                "status": "none",
                "text": "当前没有视频或作者任务。",
            }, ensure_ascii=False))
            return 0
        print(json.dumps(
            present_work(*selected), ensure_ascii=False, default=str
        ))
        return 0
    if args.command in {"work-pause", "work-resume", "work-cancel"}:
        selected = _work_row(
            repo,
            args.user_id,
            kind=args.kind,
            object_id=args.object_id,
        )
        if not selected:
            result = {
                "ok": False,
                "status": "none",
                "text": "当前没有可操作的视频或作者任务。",
            }
        else:
            kind, row = selected
            action = args.command.removeprefix("work-")
            try:
                if kind == "job":
                    if action == "pause":
                        row = repo.pause_job(row["id"], args.user_id)
                    elif action == "resume":
                        row = repo.resume_job(row["id"], args.user_id)
                    else:
                        row = repo.cancel_job(row["id"], args.user_id)
                        _cleanup_job_artifacts(settings, row)
                elif kind == "discovery":
                    if action == "pause":
                        row = repo.pause_author_discovery(
                            row["id"], args.user_id
                        )
                    elif action == "resume":
                        row = repo.resume_author_discovery(
                            row["id"], args.user_id
                        )
                    else:
                        row = repo.cancel_author_discovery(
                            row["id"], args.user_id
                        )
                elif action == "pause":
                    row = repo.pause_batch(row["id"], args.user_id)
                elif action == "resume":
                    row = repo.resume_batch(row["id"], args.user_id)
                else:
                    row = repo.request_batch_cancel(
                        row["id"], args.user_id
                    )
            except (
                BatchForbiddenError,
                BatchNotFoundError,
                BatchStateError,
                ValueError,
            ):
                result = {
                    "ok": False,
                    "status": "invalid_state",
                    "text": "当前状态不能执行此操作。",
                }
            else:
                result = present_work(kind, row)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0 if result["ok"] else 4
    if args.command == "classify":
        validate_public_url(args.url)
        try:
            result = classify_input_url(args.url)
        except ShortLinkResolutionError as exc:
            print(json.dumps({
                "kind": UrlKind.UNKNOWN.value,
                "platform": exc.platform.value,
                "normalized_url": None,
                "status": "short_link_resolution_failed",
            }))
            return 4
        print(json.dumps({
            "kind": result.kind.value,
            "platform": result.platform.value,
            "normalized_url": result.normalized_url,
        }))
        return 0
    if args.command == "discover":
        if settings.allowed_user_ids and args.user_id not in settings.allowed_user_ids:
            print(json.dumps({"ok": False, "status": "forbidden"}))
            return 3
        validate_public_url(args.url)
        try:
            result = classify_input_url(args.url)
        except ShortLinkResolutionError as exc:
            print(json.dumps({
                "ok": False,
                "status": "short_link_resolution_failed",
                "platform": exc.platform.value,
            }))
            return 4
        if result.kind != UrlKind.AUTHOR_PAGE or not result.normalized_url:
            print(json.dumps({"ok": False, "status": result.kind.value}))
            return 4
        if not author_discovery_enabled(settings, result.platform):
            print(json.dumps({
                "ok": False,
                "status": "author_discovery_disabled",
                "platform": result.platform.value,
            }))
            return 4
        row = repo.create_author_discovery(
            user_id=args.user_id,
            chat_id=args.chat_id,
            message_id=args.message_id,
            input_url=result.normalized_url,
            platform=result.platform,
            canonical_author_url=result.normalized_url,
            scan_limit=author_scan_limit(settings, result.platform),
        )
        print(json.dumps({"ok": True, "discovery_id": str(row["id"]), "state": row["state"]}))
        return 0
    if args.command == "discovery-status":
        row = repo.get_author_discovery(args.discovery_id, args.user_id)
        if not row:
            print(json.dumps({"status": "none"}))
            return 2
        print(json.dumps({
            "status": row["state"],
            "discovery_id": str(row["id"]),
            "author_name": row["author_name"],
            "discovered_count": row["discovered_count"],
            "eligible_count": row["eligible_count"],
            "existing_counts": row.get("existing_destination_counts") or {},
            "existing_main_count": row["existing_main_count"],
            "existing_cooking_count": row["existing_cooking_count"],
            "error_code": row["error_code"],
        }, ensure_ascii=False))
        return 0
    if args.command == "discovery-prompt":
        row = repo.get_author_discovery(
            args.discovery_id, args.user_id
        )
        if (
            not row
            or str(row["telegram_chat_id"]) != str(args.chat_id)
            or row["state"] not in {
                "QUEUED", "DISCOVERING", "READY", "PAUSED_USER"
            }
        ):
            print(json.dumps({"ok": False, "status": "not_ready"}))
            return 4
        notifier = TelegramNotifier()
        if not notifier.token:
            print(json.dumps({"ok": False, "status": "not_configured"}))
            return 4
        if row["state"] == "READY":
            notifier.discovery_ready(row)
        else:
            notifier.discovery_queued(row)
        print(json.dumps({"ok": True, "status": "delivered"}))
        return 0
    if args.command == "confirm-batch":
        try:
            batch = BatchConfirmationService(repo).confirm(
                args.discovery_id,
                args.user_id,
                Destination(args.destination),
                limit=args.limit,
                selection_kind=args.selection_kind,
            )
        except BatchConfirmationDisabledError:
            result = {"ok": False, "status": "execution_disabled"}
        except BatchResourceError:
            result = {"ok": False, "status": "insufficient_resources"}
        except BatchPlatformUnavailableError:
            result = {"ok": False, "status": "platform_paused"}
        except ActiveBatchExistsError as exc:
            result = {
                "ok": False,
                "status": "active_batch_exists",
                "batch_id": str(exc.batch_id),
            }
        except BatchBudgetExceededError as exc:
            result = {
                "ok": False,
                "status": "duration_budget_exceeded",
                "estimated_seconds": exc.estimated_seconds,
                "maximum_seconds": exc.maximum_seconds,
            }
        except BatchForbiddenError:
            result = {"ok": False, "status": "forbidden"}
        except BatchNotFoundError:
            result = {"ok": False, "status": "not_found"}
        except BatchStateError:
            result = {"ok": False, "status": "not_ready"}
        else:
            result = {
                "ok": True,
                "status": batch["state"],
                "batch_id": str(batch["id"]),
            }
        print(json.dumps(result))
        return 0 if result["ok"] else 4
    if args.command == "batch-preview":
        try:
            row = repo.validate_discovery_selection(
                args.discovery_id,
                args.user_id,
                args.limit,
                args.selection_kind,
            )
        except (BatchNotFoundError, BatchForbiddenError):
            result = {"ok": False, "status": "forbidden_or_expired"}
        except (BatchStateError, ValueError):
            result = {"ok": False, "status": "invalid_selection"}
        else:
            result = {
                "ok": True,
                "status": row["state"],
                "discovery_id": str(row["id"]),
                "author_name": row["author_name"],
                "limit": args.limit,
                "selection_kind": args.selection_kind,
                "existing_counts": {
                    destination: min(args.limit, int(count))
                    for destination, count in (
                        row.get("existing_destination_counts") or {}
                    ).items()
                },
                "existing_main_count": min(args.limit, row["existing_main_count"]),
                "existing_cooking_count": min(
                    args.limit, row["existing_cooking_count"]
                ),
            }
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 4
    if args.command == "batch-status":
        batch = (
            repo.get_owned_batch(args.batch_id, args.user_id)
            if args.batch_id else repo.latest_batch(args.user_id)
        )
        if not batch:
            print(json.dumps({
                "ok": False,
                "status": "none",
                "text": "还没有作者批次。",
            }, ensure_ascii=False))
            return 0
        print(json.dumps({
            "ok": True,
            "status": batch["state"],
            "batch_id": str(batch["id"]),
            "text": batch_text(batch),
        }, ensure_ascii=False))
        return 0
    if args.command in {"batch-pause", "batch-resume", "batch-cancel"}:
        try:
            if args.batch_id:
                if args.command == "batch-pause":
                    batch = repo.pause_batch(args.batch_id, args.user_id)
                elif args.command == "batch-resume":
                    batch = repo.resume_batch(args.batch_id, args.user_id)
                else:
                    batch = repo.request_batch_cancel(args.batch_id, args.user_id)
            elif args.command == "batch-pause":
                batch = repo.pause_latest_batch(args.user_id)
            elif args.command == "batch-resume":
                batch = repo.resume_latest_batch(args.user_id)
            else:
                batch = repo.cancel_latest_batch(args.user_id)
        except BatchForbiddenError:
            result = {"ok": False, "status": "forbidden"}
        except BatchNotFoundError:
            result = {"ok": False, "status": "none"}
        except BatchStateError:
            result = {"ok": False, "status": "invalid_state"}
        else:
            result = {
                "ok": True,
                "status": batch["state"],
                "batch_id": str(batch["id"]),
                "text": batch_text(batch),
            }
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 4
    if args.command == "expire-discoveries":
        print(json.dumps({"expired": repo.expire_author_discoveries()}))
        return 0
    if args.command == "probe":
        result = asyncio.run(probe_author(args.url, repo))
        print(json.dumps(result))
        return 0 if result["ok"] else 4
    if args.command == "author-acceptance":
        result = asyncio.run(author_acceptance(
            args.url,
            repo,
            limit=args.limit,
            resolve_first_video=args.resolve_first_video,
            download_first_video=args.download_first_video,
        ))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 4
    if args.command == "discovery-worker":
        asyncio.run(discovery_worker())
        return 0
    if args.command == "batch-notification-worker":
        asyncio.run(batch_notification_worker())
        return 0
    if args.command == "batch-metrics":
        print(json.dumps(repo.batch_metrics(), ensure_ascii=False, default=str))
        return 0
    if args.command == "batch-audit":
        issues = repo.audit_batch_consistency()
        print(json.dumps({
            "ok": not issues,
            "issue_count": len(issues),
            "issues": issues,
        }, ensure_ascii=False, default=str))
        return 0 if not issues else 1
    if args.command == "security-audit":
        result = sensitive_persistence_audit(repo)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1
    if args.command == "release-observation":
        try:
            services = collect_service_observation(args.hours)
        except ValueError as exc:
            print(json.dumps({
                "ok": False,
                "status": "invalid_window",
                "message": str(exc),
            }))
            return 2
        audit = repo.audit_batch_consistency()
        security = sensitive_persistence_audit(repo)
        runtime_health = health()
        metrics = repo.batch_metrics()
        result = {
            "ok": (
                services["ok"]
                and runtime_health["ok"]
                and not audit
                and security["ok"]
                and not metrics["open_circuits"]
            ),
            "observation_complete": services[
                "observation_window_complete"
            ],
            "services": services,
            "health": runtime_health,
            "batch_metrics": metrics,
            "consistency_issue_count": len(audit),
            "security_audit": security,
        }
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0 if result["ok"] else 1
    if args.command == "capabilities":
        database = database_author_batch_capabilities(repo)
        print(json.dumps(
            capability_payload(settings, database=database),
            ensure_ascii=False,
        ))
        return 0
    if args.command == "session-login":
        if args.platform == "xiaohongshu":
            from backend.xhs_session.recovery import trigger_session_refresh
        else:
            from backend.bilibili_session.recovery import (
                trigger_session_refresh,
            )
        triggered = trigger_session_refresh(settings)
        print(json.dumps({
            "ok": triggered,
            "status": "triggered" if triggered else "unavailable",
            "platform": args.platform,
        }))
        return 0 if triggered else 4
    if args.command == "pause-all-batches":
        print(json.dumps({"paused": repo.pause_all_batches()}))
        return 0
    if args.command == "worker": asyncio.run(worker()); return 0
    if args.command == "health": result = health(); print(json.dumps(result)); return 0 if result["ok"] else 1
    if args.command == "cleanup": print(json.dumps({"removed": VideoIngestionService().cleanup.purge_expired(settings.temp_root, settings.temp_file_ttl_hours)})); return 0
    return 2


if __name__ == "__main__": raise SystemExit(main())
