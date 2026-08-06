from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from backend.ingestion import cli
from backend.ingestion.config import Settings
from backend.ingestion.discovery.models import (
    AccessProbeResult,
    AuthorDiscoveryResult,
    AuthorSnapshot,
    DiscoveredWork,
)
from backend.ingestion.models import (
    AccessProbeStatus,
    DiscoveryEligibility,
    Platform,
    WorkContentType,
)


class FakeRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.successes: list[Platform] = []
        self.failures: list[tuple[Platform, str]] = []

    @staticmethod
    def acquire_platform_request(platform, *, probe=False):
        return SimpleNamespace(
            allowed=True,
            reason=None,
            retry_at=None,
            probe=probe,
            platform=Platform(platform).value,
        )

    def record_platform_success(self, platform):
        self.successes.append(Platform(platform))

    def record_platform_failure(self, platform, code, **_kwargs):
        self.failures.append((Platform(platform), code))


class FakeAdapter:
    async def probe_access(self, _url):
        return AccessProbeResult(status=AccessProbeStatus.OK)

    async def discover(self, url, *, scan_limit):
        author_id = url.rsplit("/", 1)[-1]
        return AuthorDiscoveryResult(
            author=AuthorSnapshot(
                platform=Platform.XIAOHONGSHU,
                author_id=author_id,
                canonical_url=url,
                display_name="测试作者",
            ),
            works=(
                DiscoveredWork(
                    source_id="6411cf99000000001300b6d9",
                    canonical_url=(
                        "https://www.xiaohongshu.com/explore/"
                        "6411cf99000000001300b6d9"
                    ),
                    title="测试视频",
                    published_at=None,
                    duration_seconds=12,
                    content_type=WorkContentType.VIDEO,
                    position=1,
                    eligibility=DiscoveryEligibility.ELIGIBLE,
                ),
            )[:scan_limit],
            extractor_name="fake-xiaohongshu",
            truncated=False,
        )

    async def resolve_note_media(self, author_id, note_id, *, scan_limit):
        assert scan_limit == 1
        return {
            "id": note_id,
            "title": "测试视频",
            "author_id": author_id,
            "duration": 12,
            "media_url": (
                "https://sns-video-bd.xhscdn.com/stream/video"
                "?sign=must-not-leak"
            ),
        }


def _registry(_settings):
    return SimpleNamespace(for_platform=lambda _platform: FakeAdapter())


def test_operator_probe_is_available_while_feature_flag_is_off(
    monkeypatch,
) -> None:
    settings = Settings(author_batch_xiaohongshu_enabled=False)
    repository = FakeRepository(settings)
    monkeypatch.setattr(cli, "AuthorDiscoveryAdapterRegistry", _registry)

    result = asyncio.run(cli.probe_author(
        "https://www.xiaohongshu.com/user/profile/"
        "5c31698d0000000007018a31",
        repository,
    ))

    assert result["ok"] is True
    assert result["platform"] == "xiaohongshu"
    assert repository.successes == [Platform.XIAOHONGSHU]


def test_author_acceptance_is_non_persistent_and_secret_free(
    monkeypatch,
    tmp_path,
) -> None:
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps({
        "a1": "a" * 52,
        "web_session": "must-not-leak",
    }))
    cookie_file.chmod(0o600)
    settings = Settings(
        author_batch_xiaohongshu_enabled=False,
        xiaohongshu_cookies_file=str(cookie_file),
    )
    repository = FakeRepository(settings)
    monkeypatch.setattr(cli, "AuthorDiscoveryAdapterRegistry", _registry)

    result = asyncio.run(cli.author_acceptance(
        "https://www.xiaohongshu.com/user/profile/"
        "5c31698d0000000007018a31",
        repository,
        limit=1,
        resolve_first_video=True,
    ))

    assert result["ok"] is True
    assert result["feature_flag_enabled"] is False
    assert result["persistence"] == {
        "discovery_rows_created": 0,
        "batch_rows_created": 0,
    }
    assert result["media_canary"]["status"] == "resolved"
    assert "must-not-leak" not in repr(result)


def test_author_acceptance_stops_before_network_for_insecure_cookie_file(
    tmp_path,
) -> None:
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps({
        "a1": "a" * 52,
        "web_session": "must-not-leak",
    }))
    cookie_file.chmod(0o644)
    repository = FakeRepository(Settings(
        xiaohongshu_cookies_file=str(cookie_file),
    ))

    result = asyncio.run(cli.author_acceptance(
        "https://www.xiaohongshu.com/user/profile/"
        "5c31698d0000000007018a31",
        repository,
        limit=1,
    ))

    assert result["ok"] is False
    assert result["status"] == "auth_not_ready"
    assert result["cookie_file"]["file_status"] == "insecure_permissions"
    assert "must-not-leak" not in repr(result)
