from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from backend.ingestion.config import Settings
from backend.ingestion.logging import JsonFormatter
from backend.ingestion.markdown import KnowledgeFileBuilder, fallback_enrichment
from backend.ingestion.media import (
    CleanupManager,
    MetadataExtractor,
    download_xiaohongshu_cover,
    normalize_platform,
)
from backend.ingestion.models import (
    SELECTABLE_DESTINATIONS,
    Destination,
    Platform,
    SourceMetadata,
)
from backend.ingestion.notifier import normalize_telegram_chat_id
from backend.ingestion.security import (
    SafeYtdlpLogger,
    extract_urls,
    redact_sensitive_text,
    safe_filename,
    stable_source_url,
    validate_public_url,
)
from backend.ingestion.service import (
    can_reuse_duplicate_markdown,
    requires_batch_persistence_requeue,
    should_store_video_cover,
)


def test_extract_multiple_urls() -> None:
    assert extract_urls("a https://youtu.be/a b https://bilibili.com/video/BV1.") == ["https://youtu.be/a", "https://bilibili.com/video/BV1"]


def test_only_author_batch_children_use_batch_persistence_requeue() -> None:
    assert requires_batch_persistence_requeue({
        "notification_mode": "BATCH_SILENT",
    })
    assert not requires_batch_persistence_requeue({
        "notification_mode": "INDIVIDUAL",
        "destination_locked": True,
    })


@pytest.mark.parametrize("extractor,expected", [("Youtube", Platform.YOUTUBE), ("BiliBiliVideo", Platform.BILIBILI), ("TikTok", Platform.TIKTOK), ("Xiaohongshu", Platform.XIAOHONGSHU), ("unknown", Platform.OTHER)])
def test_platform_normalization(extractor: str, expected: Platform) -> None:
    assert normalize_platform(extractor) == expected


def test_safe_filename_blocks_traversal() -> None:
    value = safe_filename("../../bad: title?.md")
    assert "/" not in value and "\\" not in value and value not in {".", ".."}


def test_ssrf_rejects_localhost() -> None:
    with pytest.raises(ValueError): validate_public_url("http://127.0.0.1/private")


def test_stable_source_url_removes_ephemeral_share_parameters() -> None:
    value = "https://www.xiaohongshu.com/discovery/item/abc?xsec_token=secret&share_id=123"
    assert stable_source_url(value, "xiaohongshu") == "https://www.xiaohongshu.com/discovery/item/abc"
    assert stable_source_url("https://www.youtube.com/watch?v=abc&utm_source=x", "youtube") == "https://www.youtube.com/watch?v=abc"


def test_sensitive_error_text_removes_url_queries_and_secret_values() -> None:
    value = (
        "failed https://user:pass@www.xiaohongshu.com/explore/abc"
        "?xsec_token=top-secret&share_id=123 "
        "access_token=another-secret"
    )
    sanitized = redact_sensitive_text(value)
    assert sanitized == (
        "failed https://www.xiaohongshu.com/explore/abc "
        "access_token=[REDACTED]"
    )
    assert "top-secret" not in sanitized
    assert "another-secret" not in sanitized
    assert "user:pass" not in sanitized


def test_sensitive_error_text_hides_xhs_cdn_capability_path() -> None:
    value = (
        "failed https://sns-video-bd.xhscdn.com/"
        "signed/private/object-key?sign=secret"
    )
    assert redact_sensitive_text(value) == (
        "failed https://sns-video-bd.xhscdn.com/[REDACTED]"
    )


def test_xiaohongshu_metadata_keeps_media_capability_transient(
    monkeypatch,
) -> None:
    extractor = MetadataExtractor(Settings())
    note_id = "6411cf99000000001300b6d9"
    author_id = "5c31698d0000000007018a31"
    calls = []

    async def resolve(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "id": note_id,
            "title": "测试视频",
            "description": "正文",
            "author": "作者",
            "author_id": author_id,
            "duration": 12.5,
            "tags": ["测试"],
            "media_url": (
                "https://sns-video-bd.xhscdn.com/"
                "private/object?sign=must-not-persist"
            ),
        }

    monkeypatch.setattr(
        "backend.ingestion.media.validate_public_url",
        lambda value: value,
    )
    monkeypatch.setattr(
        extractor.xiaohongshu, "resolve_note_media", resolve
    )
    metadata, runtime = asyncio.run(extractor.from_url(
        f"https://www.xiaohongshu.com/explore/{note_id}",
        author_id=author_id,
        author_scan_limit=0,
    ))

    assert calls[0][1]["scan_limit"] == 0
    assert metadata.platform == Platform.XIAOHONGSHU
    assert metadata.source_id == note_id
    assert metadata.uploader_id == author_id
    assert metadata.duration_seconds == pytest.approx(12.5)
    assert "media_url" not in metadata.model_dump_json()
    assert "must-not-persist" not in metadata.model_dump_json()
    assert runtime["_transient_media_url"].endswith(
        "object?sign=must-not-persist"
    )


def test_json_logging_redacts_sensitive_exception_traceback() -> None:
    try:
        raise RuntimeError(
            "unsupported https://www.xiaohongshu.com/explore/abc"
            "?xsec_token=top-secret"
        )
    except RuntimeError:
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="job failed",
            args=(),
            exc_info=__import__("sys").exc_info(),
        )
    payload = json.loads(JsonFormatter().format(record))
    assert "top-secret" not in payload["exception"]
    assert "?xsec_token" not in payload["exception"]


def test_ytdlp_logger_never_emits_raw_query_or_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="backend.ingestion.yt_dlp")
    SafeYtdlpLogger().error(
        "ERROR https://www.xiaohongshu.com/explore/abc"
        "?xsec_token=top-secret access_token=another-secret"
    )
    assert "top-secret" not in caplog.text
    assert "another-secret" not in caplog.text
    assert "?xsec_token" not in caplog.text
    assert "access_token=[REDACTED]" in caplog.text


def test_telegram_chat_id_normalizes_openclaw_prefix() -> None:
    assert normalize_telegram_chat_id("telegram:1000000001") == "1000000001"
    assert normalize_telegram_chat_id("telegram:direct:1000000001") == "1000000001"
    assert normalize_telegram_chat_id("-100123") == "-100123"


def test_completed_notification_is_explicit() -> None:
    from backend.ingestion.notifier import TelegramNotifier

    calls = []
    notifier = TelegramNotifier(token="test")
    notifier._call = lambda method, payload: calls.append((method, payload))
    notifier.completed("telegram:123", "finance", "测试视频")
    assert calls == [("sendMessage", {
        "chat_id": "telegram:123",
        "text": "✅ 录入成功\n知识库：金融与投资\n标题：测试视频\nMarkdown：已写入\nSQL：已登记\n向量索引：已完成",
    })]


def test_user_selectable_destinations_match_root_knowledge_bases() -> None:
    assert [destination.value for destination in SELECTABLE_DESTINATIONS] == [
        "thought-politics",
        "tech",
        "finance",
        "career",
        "social-conduct",
        "literature-culture",
        "general",
        "cooking",
    ]
    assert Destination.MAIN not in SELECTABLE_DESTINATIONS


def test_single_link_destination_prompt_has_all_databases() -> None:
    from backend.ingestion.notifier import TelegramNotifier

    calls = []
    notifier = TelegramNotifier(token="test")
    notifier._call = lambda method, payload: calls.append((method, payload))
    job_id = "00000000-0000-0000-0000-000000000001"
    notifier.awaiting_link("telegram:123", job_id, "youtube")

    method, payload = calls[0]
    assert method == "sendMessage"
    assert "选择后才会开始" in payload["text"]
    buttons = [
        button
        for row in payload["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert [button["text"] for button in buttons] == [
        "思想、政治与社会议题",
        "技术",
        "金融与投资",
        "职业发展",
        "中国人情世故",
        "文学与文化",
        "综合资料",
        "烹饪",
        "取消",
    ]
    assert all(len(button["callback_data"].encode()) <= 64 for button in buttons)


def test_progress_notification_is_bounded() -> None:
    from backend.ingestion.notifier import TelegramNotifier

    calls = []
    notifier = TelegramNotifier(token="test")
    notifier._call = lambda method, payload: calls.append((method, payload))
    notifier.progress("telegram:123", "x" * 800)
    assert calls[0][0] == "sendMessage"
    assert calls[0][1]["chat_id"] == "telegram:123"
    assert len(calls[0][1]["text"]) == 500


def test_deliver_telegram_notification_requires_authorized_configured_sender() -> None:
    from backend.ingestion.cli import deliver_telegram_notification
    from backend.ingestion.notifier import TelegramNotifier

    calls = []
    notifier = TelegramNotifier(token="test")
    notifier.progress = lambda chat_id, text: calls.append((chat_id, text))
    settings = Settings(allowed_user_ids=frozenset({"123"}))

    assert deliver_telegram_notification(
        user_id="999",
        chat_id="telegram:999",
        text="hidden",
        settings=settings,
        notifier=notifier,
    ) == {"ok": False, "status": "forbidden"}
    assert deliver_telegram_notification(
        user_id="123",
        chat_id="telegram:123",
        text="visible",
        settings=settings,
        notifier=notifier,
    ) == {"ok": True, "status": "delivered"}
    assert calls == [("telegram:123", "visible")]


def test_destination_prompt_requires_owned_waiting_job() -> None:
    from backend.ingestion.cli import deliver_destination_prompt
    from backend.ingestion.notifier import TelegramNotifier

    calls = []
    notifier = TelegramNotifier(token="test")
    notifier.awaiting_link = lambda *args: calls.append(args)
    repository = type(
        "Repository",
        (),
        {
            "get_job": lambda self, _job_id: {
                "telegram_user_id": "123",
                "telegram_chat_id": "telegram:123",
                "state": "AWAITING_DESTINATION",
            },
        },
    )()
    result = deliver_destination_prompt(
        user_id="123",
        chat_id="telegram:123",
        job_id="job-id",
        platform="youtube",
        settings=Settings(allowed_user_ids=frozenset({"123"})),
        repository=repository,
        notifier=notifier,
    )
    assert result == {"ok": True, "status": "delivered"}
    assert calls == [("telegram:123", "job-id", "youtube")]

    forbidden = deliver_destination_prompt(
        user_id="999",
        chat_id="telegram:123",
        job_id="job-id",
        platform="youtube",
        settings=Settings(allowed_user_ids=frozenset({"123"})),
        repository=repository,
        notifier=notifier,
    )
    assert forbidden == {"ok": False, "status": "forbidden"}


def test_deliver_start_prompt_validates_request_and_sender() -> None:
    from backend.ingestion.cli import deliver_start_prompt

    calls = []
    notifier = type("Notifier", (), {
        "token": "token",
        "start_confirmation": lambda self, chat_id, request_id: calls.append(
            (chat_id, request_id)
        ),
    })()
    settings = Settings(allowed_user_ids=frozenset({"123"}))
    request_id = str(uuid.uuid4())

    assert deliver_start_prompt(
        user_id="123",
        chat_id="456",
        request_id=request_id,
        settings=settings,
        notifier=notifier,
    ) == {"ok": True, "status": "delivered"}
    assert calls == [("456", request_id)]
    assert deliver_start_prompt(
        user_id="999",
        chat_id="456",
        request_id=request_id,
        settings=settings,
        notifier=notifier,
    ) == {"ok": False, "status": "forbidden"}
    assert deliver_start_prompt(
        user_id="123",
        chat_id="456",
        request_id="not-a-uuid",
        settings=settings,
        notifier=notifier,
    ) == {"ok": False, "status": "invalid_request"}


@pytest.mark.parametrize(
    ("staging_path", "expected_state"),
    [(None, "RECEIVED"), ("/tmp/staged.md", "PERSISTING")],
)
def test_destination_selection_starts_work_only_after_choice(
    staging_path: str | None,
    expected_state: str,
) -> None:
    from backend.ingestion.repository import SqlRepository

    writes = []

    class Connection:
        def execute(self, query, values):
            if "SELECT state" in query:
                return type("Result", (), {"fetchone": lambda self: {
                    "state": "AWAITING_DESTINATION",
                    "telegram_user_id": "123",
                    "destination_locked": False,
                    "staging_path": staging_path,
                }})()
            writes.append((query, values))
            return type("Result", (), {})()

    @contextmanager
    def factory():
        yield Connection()

    repository = SqlRepository(Settings(), connection_factory=factory)
    status = repository.select_destination(
        "00000000-0000-0000-0000-000000000001",
        "123",
        Destination.FINANCE,
    )
    assert status == "accepted"
    assert writes[0][1][:2] == (expected_state, "finance")
    assert "destination_locked=true" in writes[0][0]


def test_single_link_job_is_created_waiting_for_destination() -> None:
    from backend.ingestion.repository import SqlRepository

    inserts = []

    class Connection:
        def execute(self, query, values):
            inserts.append((query, values))
            return type(
                "Result",
                (),
                {"fetchone": lambda self: {
                    "id": values[0],
                    "state": values[-2],
                    "next_attempt_at": values[-1],
                }},
            )()

    @contextmanager
    def factory():
        yield Connection()

    repository = SqlRepository(Settings(), connection_factory=factory)
    job = repository.create_job(
        user_id="123",
        chat_id="telegram:123",
        message_id="456",
        input_kind="url",
        input_value="https://youtu.be/example",
        await_destination=True,
    )
    assert job["state"] == "AWAITING_DESTINATION"
    assert job["next_attempt_at"] is None
    assert inserts[0][1][-2:] == ("AWAITING_DESTINATION", None)


def test_batch_silent_suppresses_individual_notifications() -> None:
    from backend.ingestion.service import VideoIngestionService

    calls = []
    service = object.__new__(VideoIngestionService)
    service.notifier = type(
        "Notifier",
        (),
        {"progress": lambda self, *args: calls.append(args)},
    )()
    service._notify_job(
        {"notification_mode": "BATCH_SILENT"},
        "progress",
        "chat",
        "hidden",
    )
    service._notify_job(
        {"notification_mode": "INDIVIDUAL"},
        "progress",
        "chat",
        "shown",
    )
    assert calls == [("chat", "shown")]


def test_batch_platform_pacing_does_not_consume_failure_retry() -> None:
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from backend.ingestion.service import BatchDeferred, VideoIngestionService

    calls = []
    service = object.__new__(VideoIngestionService)
    service.sql = SimpleNamespace(
        acquire_platform_request=lambda platform: SimpleNamespace(
            allowed=False,
            retry_at=datetime.now(timezone.utc) + timedelta(seconds=15),
            reason="REQUEST_INTERVAL",
        ),
        retry_batch_child=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(BatchDeferred):
        service._ensure_batch_platform_slot({
            "id": "job-id",
            "notification_mode": "BATCH_SILENT",
            "source_platform": "xiaohongshu",
        })

    assert calls[0][1]["increment_retry"] is False


def test_system_retry_delay_defaults_to_one_minute() -> None:
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from backend.ingestion.service import VideoIngestionService

    calls = []
    service = object.__new__(VideoIngestionService)
    service.settings = Settings(job_retry_delay_seconds=60)
    service.sql = SimpleNamespace(
        retry_batch_child=lambda *args: calls.append(args)
    )
    before = datetime.now(timezone.utc)
    service._handle_batch_failure(
        {
            "id": "job-id",
            "state": "BUILDING_MARKDOWN",
            "source_platform": "bilibili",
        },
        RuntimeError("temporary failure"),
        "PROCESSING_FAILED",
        True,
    )
    delay = (calls[0][-1] - before).total_seconds()
    assert 59 <= delay <= 61


def test_individual_xiaohongshu_auth_failure_waits_for_scan_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from backend.ingestion.discovery.models import DiscoveryAdapterError
    from backend.ingestion.models import JobState
    from backend.ingestion.service import VideoIngestionService

    transitions = []
    failures = []
    notifications = []
    triggers = []
    service = object.__new__(VideoIngestionService)
    service.settings = Settings(
        xiaohongshu_session_enabled=True,
        xiaohongshu_session_root=tmp_path,
    )
    service.sql = SimpleNamespace(
        record_platform_failure=lambda *args, **kwargs: failures.append(
            (args, kwargs)
        ),
        transition=lambda *args, **kwargs: transitions.append(
            (args, kwargs)
        ),
    )
    service.notifier = SimpleNamespace(
        progress=lambda *args: notifications.append(args)
    )
    monkeypatch.setattr(
        "backend.xhs_session.recovery.trigger_session_refresh",
        lambda settings: triggers.append(settings) or True,
    )
    job = {
        "id": "job-id",
        "telegram_chat_id": "telegram:123",
        "notification_mode": "INDIVIDUAL",
        "input_kind": "url",
        "input_value": (
            "https://www.xiaohongshu.com/explore/"
            "6411cf99000000001300b6d9"
        ),
        "source_platform": None,
        "state": "DOWNLOADING",
        "retry_count": 2,
    }
    handled = service._defer_individual_xiaohongshu_auth(
        job,
        DiscoveryAdapterError("AUTH_EXPIRED", "authentication expired"),
    )
    assert handled
    assert failures[0][0][1] == "AUTH_EXPIRED"
    transition_args, transition_fields = transitions[0]
    assert transition_args == ("job-id", JobState.RECEIVED)
    assert "retry_count" not in transition_fields
    assert transition_fields["last_error_code"] == "AUTH_EXPIRED"
    assert transition_fields["source_platform"] == "xiaohongshu"
    assert transition_fields["next_attempt_at"] > datetime.now(timezone.utc)
    assert triggers == [service.settings]
    assert "扫码成功后会自动继续" in notifications[0][1]


def test_non_auth_xiaohongshu_failure_does_not_send_login_qr(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from backend.ingestion.discovery.models import DiscoveryAdapterError
    from backend.ingestion.service import VideoIngestionService

    service = object.__new__(VideoIngestionService)
    service.settings = Settings(
        xiaohongshu_session_enabled=True,
        xiaohongshu_session_root=tmp_path,
    )
    service.sql = SimpleNamespace()
    assert not service._defer_individual_xiaohongshu_auth(
        {
            "id": "job-id",
            "input_kind": "url",
            "input_value": (
                "https://www.xiaohongshu.com/explore/"
                "6411cf99000000001300b6d9"
            ),
            "state": "DOWNLOADING",
        },
        DiscoveryAdapterError("RATE_LIMITED", "rate limited"),
    )


def test_individual_bilibili_auth_failure_pauses_and_triggers_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from backend.ingestion.discovery.errors import classify_platform_error
    from backend.ingestion.models import JobState
    from backend.ingestion.service import VideoIngestionService

    transitions = []
    notifications = []
    triggers = []
    service = object.__new__(VideoIngestionService)
    service.settings = Settings(
        bilibili_session_enabled=True,
        bilibili_session_root=tmp_path,
    )
    service.sql = SimpleNamespace(
        record_platform_failure=lambda *args, **kwargs: None,
        transition=lambda *args, **kwargs: transitions.append((args, kwargs)),
    )
    service.notifier = SimpleNamespace(
        progress=lambda *args: notifications.append(args)
    )
    monkeypatch.setattr(
        "backend.bilibili_session.recovery.classify_session_error",
        lambda settings, error: classify_platform_error(
            RuntimeError("login required")
        ),
    )
    monkeypatch.setattr(
        "backend.bilibili_session.recovery.trigger_session_refresh",
        lambda settings: triggers.append(settings) or True,
    )

    handled = service._defer_individual_platform_auth(
        {
            "id": "job-id",
            "telegram_chat_id": "telegram:123",
            "notification_mode": "INDIVIDUAL",
            "input_kind": "url",
            "input_value": "https://www.bilibili.com/video/BV1xx411c7mD",
            "source_platform": "bilibili",
            "state": "EXTRACTING_SUBTITLES",
        },
        RuntimeError("HTTP Error 412: Precondition Failed"),
    )

    assert handled
    transition_args, transition_fields = transitions[0]
    assert transition_args == ("job-id", JobState.RECEIVED)
    assert transition_fields["source_platform"] == "bilibili"
    assert transition_fields["last_error_code"] == "AUTH_EXPIRED"
    assert triggers == [service.settings]
    assert "扫码成功后会自动继续" in notifications[0][1]


def test_verified_session_wakes_individual_jobs_and_author_discoveries() -> None:
    from contextlib import contextmanager

    from backend.ingestion.repository import SqlRepository

    queries = []

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class Connection:
        def execute(self, query):
            normalized = " ".join(query.split())
            queries.append(normalized)
            if "video_ingestion_jobs" in normalized:
                return Result([{"id": "job-1"}, {"id": "job-2"}])
            return Result([{"id": "discovery-1"}])

    @contextmanager
    def factory():
        yield Connection()

    repository = SqlRepository(Settings(), connection_factory=factory)
    assert repository.resume_recovered_xiaohongshu_jobs() == 2
    assert repository.resume_recovered_xiaohongshu_discoveries() == 1
    assert "notification_mode='INDIVIDUAL'" in queries[0]
    assert "source_platform='xiaohongshu'" in queries[0]
    assert "last_error_code='AUTH_EXPIRED'" in queries[0]
    assert "platform='xiaohongshu'" in queries[1]
    assert "state='QUEUED'" in queries[1]
    assert "error_code='AUTH_EXPIRED'" in queries[1]


def test_verified_bilibili_session_wakes_jobs_and_discoveries() -> None:
    from contextlib import contextmanager

    from backend.ingestion.repository import SqlRepository

    queries = []

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class Connection:
        def execute(self, query):
            normalized = " ".join(query.split())
            queries.append(normalized)
            if "video_ingestion_jobs" in normalized:
                return Result([{"id": "job-1"}])
            return Result([{"id": "discovery-1"}])

    @contextmanager
    def factory():
        yield Connection()

    repository = SqlRepository(Settings(), connection_factory=factory)
    assert repository.resume_recovered_bilibili_jobs() == 1
    assert repository.resume_recovered_bilibili_discoveries() == 1
    assert "source_platform='bilibili'" in queries[0]
    assert "platform='bilibili'" in queries[1]


def test_markdown_frontmatter_and_full_transcript(tmp_path: Path) -> None:
    meta = SourceMetadata(platform=Platform.TELEGRAM, source_id="abc", original_title="测试", author=None, language="zh", duration_seconds=12)
    transcript = "# Video Transcription\n\n## Transcription Content\n\n**[00:00 - 00:02]**\n\n完整原文"
    enrichment = fallback_enrichment("测试", transcript)
    text, checksum = KnowledgeFileBuilder().build(document_id="doc", metadata=meta, enrichment=enrichment, transcript=transcript, telegram_chat_id="1", telegram_message_id="2")
    raw = text.split("---", 2)[1]
    frontmatter = yaml.safe_load(raw)
    assert frontmatter["source_platform"] == "telegram"
    assert frontmatter["checksum"] == checksum
    assert frontmatter["lifecycle"] == "curated"
    assert frontmatter["domain"] == "reference"
    assert "视频归档" in frontmatter["retrieval_aliases"]
    assert "完整原文" in text and "## Transcript" in text


def test_xiaohongshu_markdown_embeds_local_cover_and_clickable_source() -> None:
    meta = SourceMetadata(
        platform=Platform.XIAOHONGSHU,
        source_id="abc",
        canonical_url="https://www.xiaohongshu.com/explore/abc",
        original_title="菜谱视频",
    )
    enrichment = fallback_enrichment("菜谱视频", "完整菜谱转录内容。" * 3)
    text, _ = KnowledgeFileBuilder().build(
        document_id="doc",
        metadata=meta,
        enrichment=enrichment,
        transcript="完整菜谱转录内容",
        telegram_chat_id="1",
        telegram_message_id="2",
        cover_filename="菜谱视频--doc-cover.jpg",
    )
    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    assert frontmatter["cover_image"] == "菜谱视频--doc-cover.jpg"
    assert "![[菜谱视频--doc-cover.jpg]]" in text
    assert (
        "[在小红书查看原视频]"
        "(https://www.xiaohongshu.com/explore/abc)"
    ) in text


def test_xiaohongshu_cover_is_normalized_to_jpeg(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class Headers:
        @staticmethod
        def get(name):
            return "image/webp" if name == "Content-Type" else None

    class Response:
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read(_maximum):
            return b"webp-image"

    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(b"jpeg-image")
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(
        "backend.ingestion.media.urllib.request.urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    monkeypatch.setattr(
        "backend.ingestion.media.subprocess.run",
        fake_run,
    )

    cover = asyncio.run(download_xiaohongshu_cover(
        "https://sns-webpic-qc.xhscdn.com/cover.webp?sign=temporary",
        tmp_path,
    ))

    assert cover == tmp_path / "cover.jpg"
    assert cover.read_bytes() == b"jpeg-image"
    assert not (tmp_path / "cover-source").exists()


@pytest.mark.parametrize(
    ("destination", "expected"),
    [
        (Destination.COOKING.value, True),
        (Destination.GENERAL.value, False),
        (Destination.TECH.value, False),
        (None, False),
    ],
)
def test_video_cover_is_stored_only_for_cooking(
    destination: str | None,
    expected: bool,
) -> None:
    assert should_store_video_cover(
        {"selected_destination": destination},
        Platform.XIAOHONGSHU,
    ) is expected
    assert should_store_video_cover(
        {"selected_destination": destination},
        Platform.YOUTUBE,
    ) is False


def test_xiaohongshu_markdown_is_not_reused_across_destinations() -> None:
    assert can_reuse_duplicate_markdown(Platform.XIAOHONGSHU) is False
    assert can_reuse_duplicate_markdown(Platform.YOUTUBE) is True
    assert can_reuse_duplicate_markdown(Platform.BILIBILI) is True


def test_domain_inference_marks_career_video() -> None:
    meta = SourceMetadata(platform=Platform.XIAOHONGSHU, original_title="领英求职内推方法")
    enrichment = fallback_enrichment(meta.original_title, "如何在 LinkedIn 联系招聘人员并获得内推。" * 5)
    text, _ = KnowledgeFileBuilder().build(document_id="doc", metadata=meta, enrichment=enrichment, transcript="完整转录内容", telegram_chat_id="1", telegram_message_id="2")
    assert yaml.safe_load(text.split("---", 2)[1])["domain"] == "career"


def test_domain_inference_marks_social_conduct_video() -> None:
    meta = SourceMetadata(
        platform=Platform.XIAOHONGSHU,
        original_title="体制内饭局敬酒与说话艺术",
    )
    enrichment = fallback_enrichment(
        meta.original_title,
        "介绍中国人情世故、沟通分寸和为人处世。" * 5,
    )
    text, _ = KnowledgeFileBuilder().build(
        document_id="doc",
        metadata=meta,
        enrichment=enrichment,
        transcript="完整转录内容",
        telegram_chat_id="1",
        telegram_message_id="2",
    )
    assert yaml.safe_load(text.split("---", 2)[1])["domain"] == "social-conduct"


@pytest.mark.parametrize(
    "title",
    [
        "终面时大领导最关注的5件事",
        "老板坦白局：3个不升你的心里话",
    ],
)
def test_domain_inference_marks_career_language(title: str) -> None:
    meta = SourceMetadata(platform=Platform.XIAOHONGSHU, original_title=title)
    enrichment = fallback_enrichment(title, "完整转录内容。")
    text, _ = KnowledgeFileBuilder().build(
        document_id="doc",
        metadata=meta,
        enrichment=enrichment,
        transcript="完整转录内容",
        telegram_chat_id="1",
        telegram_message_id="2",
    )
    assert yaml.safe_load(text.split("---", 2)[1])["domain"] == "career"


def test_domain_inference_routes_known_career_author() -> None:
    meta = SourceMetadata(
        platform=Platform.XIAOHONGSHU,
        original_title="一个华裔小孩的33年",
        author="Mr Jonathan",
    )
    enrichment = fallback_enrichment(meta.original_title, "完整转录内容。")
    text, _ = KnowledgeFileBuilder().build(
        document_id="doc",
        metadata=meta,
        enrichment=enrichment,
        transcript="完整转录内容",
        telegram_chat_id="1",
        telegram_message_id="2",
    )
    assert yaml.safe_load(text.split("---", 2)[1])["domain"] == "career"


def test_enrichment_schema_has_meaningful_tags() -> None:
    value = fallback_enrichment("Python 教程", "Python asyncio 数据库 FastAPI。Python 并发任务和 PostgreSQL。")
    assert 3 <= len(value.tags) <= 20


def test_cleanup_preserves_requested_file(tmp_path: Path) -> None:
    job = tmp_path / "job"; job.mkdir(); media = job / "a.mp4"; keep = job / "keep.txt"
    media.write_bytes(b"x"); keep.write_text("x")
    CleanupManager().cleanup_media(job, {keep})
    assert not media.exists() and keep.exists()


def test_chunking_reuses_main_knowledge_implementation() -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location("rag", "/home/ubuntu/services/rag-app/rag.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    chunks = module.chunk_text("中文段落。" * 500)
    assert len(chunks) > 1 and all(len(chunk) <= module.MAX_CHUNK_CHARACTERS for chunk in chunks)


def test_destination_roots_are_existing_knowledge_bases(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.ingestion.config import Settings
    settings = Settings()
    assert str(settings.main_markdown_root).startswith(
        "/home/ubuntu/知识库/General Resources"
    )
    assert str(settings.cooking_markdown_root).startswith("/home/ubuntu/知识库/Cooking")


def test_unified_indexing_service_routes_all_eight_knowledge_bases() -> None:
    from backend.ingestion.destinations import IndexResult, UnifiedIndexingService

    class RecordingIndexer:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Path]] = []

        def ingest(
            self,
            *,
            knowledge_base: str,
            markdown_path: Path,
        ) -> IndexResult:
            self.calls.append((knowledge_base, markdown_path))
            return IndexResult(knowledge_base=knowledge_base, indexed=True)

    topic = RecordingIndexer()
    cooking = RecordingIndexer()
    service = UnifiedIndexingService(topic, cooking)
    path = Path("/tmp/example.md")

    for knowledge_base in (
        "thought-politics",
        "tech",
        "finance",
        "career",
        "social-conduct",
        "literature-culture",
        "general",
    ):
        result = service.ingest(
            knowledge_base=knowledge_base,
            markdown_path=path,
        )
        assert result.knowledge_base == knowledge_base
    service.ingest(knowledge_base="cooking", markdown_path=path)

    assert [item[0] for item in topic.calls] == [
        "thought-politics",
        "tech",
        "finance",
        "career",
        "social-conduct",
        "literature-culture",
        "general",
    ]
    assert cooking.calls == [("cooking", path)]


def test_unified_indexing_service_rejects_unknown_knowledge_base() -> None:
    from backend.ingestion.destinations import UnifiedIndexingService

    service = UnifiedIndexingService(object(), object())
    with pytest.raises(ValueError):
        service.ingest(
            knowledge_base="unknown",
            markdown_path=Path("/tmp/example.md"),
        )


@pytest.mark.parametrize(
    ("destination", "expected_parent"),
    [
        (Destination.THOUGHT_POLITICS, "Thought and Politics/4. Video Transcripts"),
        (Destination.TECH, "Technology/3. Video Transcripts"),
        (Destination.FINANCE, "Finance and Investment/2. Video Transcripts"),
        (Destination.CAREER, "Career Development/2. Video Transcripts"),
        (
            Destination.SOCIAL_CONDUCT,
            "Chinese Social Relations and Conduct/2. Video Transcripts",
        ),
        (
            Destination.LITERATURE_CULTURE,
            "Literature and Culture/3. Video Transcripts",
        ),
        (Destination.GENERAL, "General Resources/2. Video Transcripts"),
        (Destination.COOKING, "Cooking/5. Video Transcripts"),
    ],
)
def test_explicit_destination_controls_markdown_path(
    destination: Destination,
    expected_parent: str,
) -> None:
    from backend.ingestion.destinations import KnowledgeDestinationService

    service = KnowledgeDestinationService(Settings(), vectors=object())
    target = service.target_path(
        destination,
        "测试标题",
        "00000000-0000-0000-0000-000000000001",
        metadata={"domain": "finance"},
    )
    assert str(target.parent).endswith(expected_parent)


def test_main_destination_routes_lao_zhou_to_politics(tmp_path: Path) -> None:
    from backend.ingestion.destinations import classify_main_markdown

    markdown = tmp_path / "video.md"
    markdown.write_text(
        "---\nauthor: 老周横眉\ndomain: finance\n---\n\n正文",
        encoding="utf-8",
    )
    assert classify_main_markdown(markdown) == "thought-politics"


def test_main_destination_routes_technology_by_domain(tmp_path: Path) -> None:
    from backend.ingestion.destinations import classify_main_markdown

    markdown = tmp_path / "video.md"
    markdown.write_text(
        "---\nauthor: 李老师\ndomain: technology\n---\n\n正文",
        encoding="utf-8",
    )
    assert classify_main_markdown(markdown) == "tech"


def test_main_destination_routes_known_career_author(tmp_path: Path) -> None:
    from backend.ingestion.destinations import classify_main_markdown

    markdown = tmp_path / "video.md"
    markdown.write_text(
        "---\nauthor: Mr Jonathan\ndomain: reference\n---\n\n正文",
        encoding="utf-8",
    )
    assert classify_main_markdown(markdown) == "career"


def test_worker_startup_requests_immediate_orphan_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from backend.ingestion import cli

    calls: list[int] = []

    class FakeSql:
        def recover_stale(self, stale_after_seconds: int) -> None:
            calls.append(stale_after_seconds)

        def claim_next(self):
            raise asyncio.CancelledError

        def reconcile_batches(self):
            calls.append(-1)

    class FakeService:
        def __init__(self):
            self.sql = FakeSql()

    monkeypatch.setattr(cli, "VideoIngestionService", FakeService)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cli.worker())
    assert calls == [0, -1]
