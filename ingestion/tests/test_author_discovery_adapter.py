from __future__ import annotations

import asyncio
import json
import os
from datetime import timezone

import pytest

from backend.ingestion.config import Settings
from backend.ingestion.discovery import bilibili as bilibili_module
from backend.ingestion.discovery import xiaohongshu as xiaohongshu_module
from backend.ingestion.discovery import youtube as youtube_module
from backend.ingestion.discovery.bilibili import BilibiliAuthorDiscoveryAdapter
from backend.ingestion.discovery.models import DiscoveryAdapterError
from backend.ingestion.discovery.url_classifier import (
    ShortLinkResolutionError,
    classify_input_url,
    classify_url,
    normalize_author_url,
    normalize_bilibili_author_url,
    normalize_xiaohongshu_author_url,
)
from backend.ingestion.discovery.xiaohongshu import (
    XiaohongshuAuthorDiscoveryAdapter,
    cookie_auth_status,
)
from backend.ingestion.discovery.youtube import YouTubeAuthorDiscoveryAdapter
from backend.ingestion.models import (
    AccessProbeStatus,
    DiscoveryEligibility,
    PlatformErrorCode,
    UrlKind,
    WorkContentType,
)


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://www.youtube.com/watch?v=abcdefghijk", UrlKind.SINGLE_WORK),
        ("https://youtu.be/abcdefghijk", UrlKind.SINGLE_WORK),
        ("https://youtube.com/shorts/abcdefghijk", UrlKind.SINGLE_WORK),
        ("https://youtube.com/live/abcdefghijk", UrlKind.SINGLE_WORK),
        ("https://youtube.com/@Example", UrlKind.AUTHOR_PAGE),
        ("https://youtube.com/@Example/videos?view=0", UrlKind.AUTHOR_PAGE),
        ("https://youtube.com/channel/UC_123/videos", UrlKind.AUTHOR_PAGE),
        ("https://youtube.com/c/example", UrlKind.AUTHOR_PAGE),
        ("https://youtube.com/user/example/videos", UrlKind.AUTHOR_PAGE),
        ("https://youtube.com/playlist?list=PL_123", UrlKind.UNSUPPORTED_COLLECTION),
        ("https://youtube.com/results?search_query=test", UrlKind.UNSUPPORTED_COLLECTION),
        ("https://youtube.com/feed/subscriptions", UrlKind.UNSUPPORTED_COLLECTION),
        ("https://youtube.com/@Example/shorts", UrlKind.UNSUPPORTED_COLLECTION),
        ("https://youtube.com/watch?list=PL_123", UrlKind.UNKNOWN),
        ("https://example.com/@Example", UrlKind.UNKNOWN),
    ],
)
def test_youtube_url_classification(url: str, kind: UrlKind) -> None:
    assert classify_url(url).kind == kind


def test_author_url_normalization_drops_query_and_known_tab() -> None:
    assert normalize_author_url(
        "http://m.youtube.com/@Example/videos?view=0#top"
    ) == "https://www.youtube.com/@Example/videos"
    assert normalize_author_url(
        "https://youtube.com/channel/UC_123"
    ) == "https://www.youtube.com/channel/UC_123/videos"
    with pytest.raises(ValueError):
        normalize_author_url("https://youtube.com/playlist?list=x")


@pytest.mark.parametrize(
    ("url", "kind", "platform"),
    [
        (
            "https://space.bilibili.com/12345",
            UrlKind.AUTHOR_PAGE,
            "bilibili",
        ),
        (
            "https://space.bilibili.com/12345/upload/video?from=space",
            UrlKind.AUTHOR_PAGE,
            "bilibili",
        ),
        (
            "https://m.bilibili.com/space/12345?share_from=space",
            UrlKind.AUTHOR_PAGE,
            "bilibili",
        ),
        (
            "https://www.bilibili.com/video/BV1xx411c7mD",
            UrlKind.SINGLE_WORK,
            "bilibili",
        ),
        (
            "https://space.bilibili.com/12345/favlist",
            UrlKind.UNSUPPORTED_COLLECTION,
            "bilibili",
        ),
        (
            "https://www.xiaohongshu.com/user/profile/5c31698d0000000007018a31?xsec_token=secret",
            UrlKind.AUTHOR_PAGE,
            "xiaohongshu",
        ),
        (
            "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9",
            UrlKind.SINGLE_WORK,
            "xiaohongshu",
        ),
        (
            "https://www.xiaohongshu.com/discovery/item/674051740000000007027a15?xsec_token=secret",
            UrlKind.SINGLE_WORK,
            "xiaohongshu",
        ),
        (
            "https://www.xiaohongshu.com/search_result?keyword=test",
            UrlKind.UNSUPPORTED_COLLECTION,
            "xiaohongshu",
        ),
    ],
)
def test_bilibili_and_xiaohongshu_url_classification(
    url: str, kind: UrlKind, platform: str
) -> None:
    result = classify_url(url)
    assert result.kind == kind
    assert result.platform.value == platform


def test_multiplatform_author_normalization_drops_transient_query() -> None:
    assert normalize_bilibili_author_url(
        "http://space.bilibili.com/12345/upload/video?from=space"
    ) == "https://space.bilibili.com/12345/video"
    assert normalize_bilibili_author_url(
        "https://m.bilibili.com/space/12345?share_from=space"
    ) == "https://space.bilibili.com/12345/video"
    normalized = normalize_xiaohongshu_author_url(
        "https://www.xiaohongshu.com/user/profile/"
        "5c31698d0000000007018a31?xsec_token=must-not-persist"
    )
    assert normalized == (
        "https://www.xiaohongshu.com/user/profile/"
        "5c31698d0000000007018a31"
    )
    assert "xsec" not in normalized
    single = classify_url(
        "https://www.xiaohongshu.com/discovery/item/"
        "674051740000000007027a15?xsec_token=must-not-persist"
    )
    assert single.normalized_url == (
        "https://www.xiaohongshu.com/explore/674051740000000007027a15"
    )
    assert "xsec" not in single.normalized_url
    assert "xsec_token=must-not-persist" in single.transient_access_url


class FakeRedirectResponse:
    def __init__(self, location: str):
        self.status = 302
        self.headers = {"Location": location}
        self.closed = False

    def close(self):
        self.closed = True


class FakeRedirectOpener:
    def __init__(self, location: str):
        self.response = FakeRedirectResponse(location)
        self.request = None
        self.timeout = None

    def open(self, request, *, timeout):
        self.request = request
        self.timeout = timeout
        return self.response


def test_xiaohongshu_share_link_resolves_to_query_free_author_url(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "backend.ingestion.discovery.url_classifier.validate_public_url",
        lambda value: value,
    )
    opener = FakeRedirectOpener(
        "https://www.xiaohongshu.com/user/profile/"
        "5fc40fdf000000000100a66b?xsec_token=must-not-persist"
    )

    result = classify_input_url(
        "https://xhslink.cn/m/1Wn0JZbpBH6",
        opener=opener,
    )

    assert result.kind == UrlKind.AUTHOR_PAGE
    assert result.platform == "xiaohongshu"
    assert result.normalized_url == (
        "https://www.xiaohongshu.com/user/profile/"
        "5fc40fdf000000000100a66b"
    )
    assert "xsec_token" not in result.normalized_url
    assert opener.request.get_header("User-agent")
    assert opener.response.closed is True


def test_xiaohongshu_share_link_rejects_non_xiaohongshu_target(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "backend.ingestion.discovery.url_classifier.validate_public_url",
        lambda value: value,
    )
    opener = FakeRedirectOpener("https://example.com/video")

    with pytest.raises(ShortLinkResolutionError):
        classify_input_url(
            "http://xhslink.com/m/AVYkakMvn50",
            opener=opener,
        )

    assert opener.response.closed is True


def test_bilibili_share_link_resolves_mobile_author_url(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "backend.ingestion.discovery.url_classifier.validate_public_url",
        lambda value: value,
    )
    opener = FakeRedirectOpener(
        "https://m.bilibili.com/space/2062919936"
        "?share_from=space&unique_k=must-not-persist"
    )

    result = classify_input_url(
        "https://b23.tv/auTHnyF",
        opener=opener,
    )

    assert result.kind == UrlKind.AUTHOR_PAGE
    assert result.platform == "bilibili"
    assert result.normalized_url == (
        "https://space.bilibili.com/2062919936/video"
    )
    assert "must-not-persist" not in repr(result)
    assert opener.request.get_header("User-agent")
    assert opener.response.closed is True


def test_bilibili_share_link_rejects_non_bilibili_target(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "backend.ingestion.discovery.url_classifier.validate_public_url",
        lambda value: value,
    )
    opener = FakeRedirectOpener("https://example.com/video")

    with pytest.raises(ShortLinkResolutionError) as error:
        classify_input_url(
            "https://b23.tv/unsafe",
            opener=opener,
        )

    assert error.value.platform == "bilibili"
    assert opener.response.closed is True


class FakeYdl:
    def __init__(self, options, info, captured):
        self.options = options
        self.info = info
        self.captured = captured

    def __enter__(self):
        self.captured["options"] = self.options
        return self

    def __exit__(self, *_):
        return None

    def extract_info(self, url, download):
        self.captured["url"] = url
        self.captured["download"] = download
        return self.info


def test_flat_discovery_contract_and_metadata_allowlist(monkeypatch) -> None:
    monkeypatch.setattr(youtube_module, "validate_public_url", lambda value: value)
    captured = {}
    info = {
        "_type": "playlist",
        "id": "UC_STABLE_123",
        "channel_id": "UC_STABLE_123",
        "channel": "Example Channel",
        "extractor_key": "YoutubeTab",
        "entries": iter(
            [
                {
                    "id": "abcdefghijk",
                    "title": "Newest video",
                    "duration": 61,
                    "timestamp": 1_700_000_000,
                    "url": "https://www.youtube.com/watch?v=abcdefghijk",
                    "cookie": "must-not-persist",
                    "xsec_token": "must-not-persist",
                },
                {
                    "id": "lmnopqrstuv",
                    "title": "Live now",
                    "live_status": "is_live",
                },
                {
                    "id": "zyxwvutsrqp",
                    "title": "Replay",
                    "live_status": "was_live",
                },
                {"id": "invalid id", "title": "Invalid"},
                {"id": "abcdefghijk", "title": "Duplicate"},
            ]
        ),
    }
    adapter = YouTubeAuthorDiscoveryAdapter(
        Settings(),
        ydl_factory=lambda options: FakeYdl(options, info, captured),
    )
    result = asyncio.run(
        adapter.discover("https://youtube.com/@Example", scan_limit=5)
    )

    assert result.author.author_id == "UC_STABLE_123"
    assert result.author.canonical_url == "https://www.youtube.com/@Example/videos"
    assert [work.source_id for work in result.works] == [
        "abcdefghijk",
        "lmnopqrstuv",
        "zyxwvutsrqp",
    ]
    assert result.works[0].published_at.tzinfo == timezone.utc
    assert result.works[1].eligibility == DiscoveryEligibility.UNAVAILABLE
    assert result.works[2].content_type == WorkContentType.LIVE_REPLAY
    assert result.works[0].canonical_url == (
        "https://www.youtube.com/watch?v=abcdefghijk"
    )
    assert "cookie" not in result.works[0].raw_metadata
    assert "xsec_token" not in result.works[0].raw_metadata
    assert captured["download"] is False
    assert captured["options"]["extract_flat"] == "in_playlist"
    assert captured["options"]["playlistend"] == 5
    assert captured["options"]["skip_download"] is True
    assert captured["options"]["cachedir"] is False
    assert captured["options"]["writesubtitles"] is False
    assert "outtmpl" not in captured["options"]


def test_adapter_rejects_single_video_and_missing_author_identity(monkeypatch) -> None:
    monkeypatch.setattr(youtube_module, "validate_public_url", lambda value: value)
    for info in (
        {"_type": "video", "id": "abcdefghijk"},
        {"_type": "playlist", "entries": []},
    ):
        adapter = YouTubeAuthorDiscoveryAdapter(
            Settings(),
            ydl_factory=lambda options, info=info: FakeYdl(options, info, {}),
        )
        with pytest.raises(DiscoveryAdapterError) as error:
            asyncio.run(
                adapter.discover("https://youtube.com/@Example", scan_limit=1)
            )
        assert error.value.code == PlatformErrorCode.EXTRACTOR_SCHEMA_CHANGED.value


def test_duration_limit_marks_item_unsupported(monkeypatch) -> None:
    monkeypatch.setattr(youtube_module, "validate_public_url", lambda value: value)
    info = {
        "_type": "playlist",
        "channel_id": "UC_STABLE_123",
        "entries": [{"id": "abcdefghijk", "duration": 14401}],
    }
    adapter = YouTubeAuthorDiscoveryAdapter(
        Settings(),
        ydl_factory=lambda options: FakeYdl(options, info, {}),
    )
    result = asyncio.run(
        adapter.discover("https://youtube.com/@Example", scan_limit=1)
    )
    assert result.works[0].eligibility == DiscoveryEligibility.UNSUPPORTED


def test_bilibili_flat_discovery_uses_stable_ids_and_safe_options(
    monkeypatch,
) -> None:
    monkeypatch.setattr(bilibili_module, "validate_public_url", lambda value: value)
    captured = {}
    info = {
        "_type": "playlist",
        "id": "12345",
        "uploader_id": "12345",
        "uploader": "Bili Author",
        "extractor_key": "BilibiliSpaceVideo",
        "entries": iter([
            {
                "id": "BV1xx411c7mD",
                "title": "Newest",
                "duration": 42,
                "timestamp": 1_700_000_000,
                "cookie": "must-not-persist",
            },
            {
                "id": "av123456",
                "title": "Older",
                "availability": "needs_auth",
            },
            {"id": "invalid"},
            {"id": "BV1xx411c7mD", "title": "Duplicate"},
        ]),
    }
    adapter = BilibiliAuthorDiscoveryAdapter(
        Settings(),
        ydl_factory=lambda options: FakeYdl(options, info, captured),
    )
    result = asyncio.run(
        adapter.discover("https://space.bilibili.com/12345", scan_limit=4)
    )
    assert result.author.author_id == "12345"
    assert result.author.canonical_url == (
        "https://space.bilibili.com/12345/video"
    )
    assert [work.source_id for work in result.works] == [
        "BV1xx411c7mD",
        "av123456",
    ]
    assert result.works[1].eligibility == DiscoveryEligibility.UNAVAILABLE
    assert result.works[0].canonical_url == (
        "https://www.bilibili.com/video/BV1xx411c7mD"
    )
    assert "cookie" not in result.works[0].raw_metadata
    assert captured["options"]["extract_flat"] == "in_playlist"
    assert captured["options"]["skip_download"] is True
    assert captured["download"] is False


def test_xiaohongshu_initial_state_is_bounded_and_never_persists_token(
    monkeypatch,
) -> None:
    monkeypatch.setattr(xiaohongshu_module, "validate_public_url", lambda value: value)
    user_id = "5c31698d0000000007018a31"
    video_id = "6411cf99000000001300b6d9"
    image_id = "674051740000000007027a15"
    html = f"""
    <script>
    window.__INITIAL_STATE__ = {{
      user: {{ userId: '{user_id}', nickname: '小红书作者' }},
      notes: [
        {{ noteCard: {{
          noteId: '{video_id}', displayTitle: '视频笔记',
          type: 'video', time: 1700000000000,
          video: {{ duration: 101726 }},
          xsecToken: 'must-not-persist'
        }} }},
        {{ noteCard: {{
          noteId: '{image_id}', displayTitle: '图文笔记',
          type: 'normal', xsec_token: 'must-not-persist'
        }} }}
      ],
      optional: undefined
    }};
    </script>
    """
    requested = []
    adapter = XiaohongshuAuthorDiscoveryAdapter(
        Settings(),
        html_fetcher=lambda url: requested.append(url) or html,
    )
    result = asyncio.run(
        adapter.discover(
            f"https://www.xiaohongshu.com/user/profile/{user_id}"
            "?xsec_token=transient",
            scan_limit=2,
        )
    )
    assert requested == [
        f"https://www.xiaohongshu.com/user/profile/{user_id}"
    ]
    assert result.author.author_id == user_id
    assert result.author.display_name == "小红书作者"
    assert [work.source_id for work in result.works] == [video_id, image_id]
    assert result.works[0].duration_seconds == pytest.approx(101.726)
    assert result.works[0].eligibility == DiscoveryEligibility.ELIGIBLE
    assert result.works[1].eligibility == DiscoveryEligibility.UNSUPPORTED
    serialized = repr(result)
    assert "xsec" not in serialized
    assert "must-not-persist" not in serialized
    assert "transient" not in serialized


def test_youtube_and_bilibili_unlimited_discovery_omit_playlist_end(
    monkeypatch,
) -> None:
    monkeypatch.setattr(youtube_module, "validate_public_url", lambda value: value)
    monkeypatch.setattr(bilibili_module, "validate_public_url", lambda value: value)
    youtube_capture = {}
    bilibili_capture = {}
    youtube_info = {
        "_type": "playlist",
        "channel_id": "UC_STABLE_123",
        "entries": [
            {"id": f"videoid{index:04d}"}
            for index in range(75)
        ],
    }
    bilibili_info = {
        "_type": "playlist",
        "uploader_id": "12345",
        "entries": [
            {"id": f"av{index + 1}"}
            for index in range(75)
        ],
    }
    youtube = YouTubeAuthorDiscoveryAdapter(
        Settings(),
        ydl_factory=lambda options: FakeYdl(
            options, youtube_info, youtube_capture
        ),
    )
    bilibili = BilibiliAuthorDiscoveryAdapter(
        Settings(),
        ydl_factory=lambda options: FakeYdl(
            options, bilibili_info, bilibili_capture
        ),
    )

    youtube_result = asyncio.run(youtube.discover(
        "https://youtube.com/@Example", scan_limit=0
    ))
    bilibili_result = asyncio.run(bilibili.discover(
        "https://space.bilibili.com/12345", scan_limit=0
    ))

    assert len(youtube_result.works) == 75
    assert len(bilibili_result.works) == 75
    assert youtube_result.truncated is False
    assert bilibili_result.truncated is False
    assert "playlistend" not in youtube_capture["options"]
    assert "playlistend" not in bilibili_capture["options"]


def test_xiaohongshu_signed_pages_reach_youtube_limit_without_token_leak(
    monkeypatch,
) -> None:
    monkeypatch.setattr(xiaohongshu_module, "validate_public_url", lambda value: value)
    user_id = "5c31698d0000000007018a31"
    ids = [f"6411cf9{index:x}000000001300b6d9" for index in range(5)]
    html = f"""
    <script>
    window.__INITIAL_STATE__ = {{
      user: {{ userId: '{user_id}', nickname: '分页作者' }},
      notes: {{
        noteList: [
          {{ noteCard: {{ noteId: '{ids[0]}', type: 'video' }} }},
          {{ noteCard: {{ noteId: '{ids[1]}', type: 'normal' }} }}
        ],
        cursor: 'cursor-1',
        hasMore: true
      }}
    }};
    </script>
    """
    calls = []

    def fetch_page(author_id: str, cursor: str, page_size: int):
        calls.append((author_id, cursor, page_size))
        if cursor == "cursor-1":
            return {
                "notes": [
                    {
                        "note_id": ids[1],
                        "type": "normal",
                        "xsec_token": "must-not-persist",
                    },
                    {
                        "note_id": ids[2],
                        "display_title": "第三条",
                        "type": "video",
                        "xsec_token": "must-not-persist",
                    },
                ],
                "cursor": "cursor-2",
                "has_more": True,
            }
        return {
            "notes": [
                {"note_id": ids[3], "type": "video"},
                {"note_id": ids[4], "type": "video"},
            ],
            "cursor": "cursor-3",
            "has_more": False,
        }

    adapter = XiaohongshuAuthorDiscoveryAdapter(
        Settings(),
        html_fetcher=lambda _: html,
        page_fetcher=fetch_page,
    )
    result = asyncio.run(adapter.discover(
        f"https://www.xiaohongshu.com/user/profile/{user_id}",
        scan_limit=5,
    ))

    assert [work.source_id for work in result.works] == ids
    assert calls == [
        (user_id, "cursor-1", 3),
        (user_id, "cursor-2", 2),
    ]
    assert result.extractor_name == "xiaohongshu-signed-user-posted"
    assert result.truncated is True
    assert result.works[1].eligibility == DiscoveryEligibility.UNSUPPORTED
    assert "xsec" not in repr(result)
    assert "must-not-persist" not in repr(result)


def test_xiaohongshu_unlimited_discovery_reads_until_feed_is_exhausted(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        xiaohongshu_module,
        "validate_public_url",
        lambda value: value,
    )
    user_id = "5c31698d0000000007018a31"
    html = """
    <script>
    window.__INITIAL_STATE__ = {
      user: { notes: { noteList: [], cursor: '', hasMore: true } }
    };
    </script>
    """
    calls = []

    def fetch_page(author_id: str, cursor: str, page_size: int):
        page = len(calls)
        calls.append((author_id, cursor, page_size))
        return {
            "notes": [{
                "note_id": f"{page + 1:024x}",
                "type": "video",
            }],
            "cursor": f"cursor-{page + 1}",
            "has_more": page < 11,
        }

    adapter = XiaohongshuAuthorDiscoveryAdapter(
        Settings(),
        html_fetcher=lambda _: html,
        page_fetcher=fetch_page,
    )
    result = asyncio.run(adapter.discover(
        f"https://www.xiaohongshu.com/user/profile/{user_id}",
        scan_limit=0,
    ))

    assert len(calls) == 12
    assert len(result.works) == 12
    assert all(call[2] == xiaohongshu_module.API_PAGE_SIZE for call in calls)
    assert result.truncated is False


def test_xiaohongshu_signed_api_overrides_empty_terminal_ssr(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        xiaohongshu_module,
        "validate_public_url",
        lambda value: value,
    )
    user_id = "5c31698d0000000007018a31"
    note_id = "6411cf99000000001300b6d9"
    html = """
    <script>
    window.__INITIAL_STATE__ = {
      user: {
        notes: { noteList: [], cursor: '', hasMore: false }
      }
    };
    </script>
    """
    calls = []

    def fetch_page(author_id: str, cursor: str, page_size: int):
        calls.append((author_id, cursor, page_size))
        return {
            "notes": [{"note_id": note_id, "type": "video"}],
            "cursor": "done",
            "has_more": False,
        }

    adapter = XiaohongshuAuthorDiscoveryAdapter(
        Settings(),
        html_fetcher=lambda _: html,
        page_fetcher=fetch_page,
    )
    result = asyncio.run(adapter.discover(
        f"https://www.xiaohongshu.com/user/profile/{user_id}",
        scan_limit=1,
    ))

    assert calls == [(user_id, "", 1)]
    assert [work.source_id for work in result.works] == [note_id]
    assert result.extractor_name == "xiaohongshu-signed-user-posted"


def test_xiaohongshu_signed_api_recovers_when_ssr_state_is_missing(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        xiaohongshu_module,
        "validate_public_url",
        lambda value: value,
    )
    user_id = "5c31698d0000000007018a31"
    note_id = "6411cf99000000001300b6d9"
    calls = []

    def fetch_page(author_id: str, cursor: str, page_size: int):
        calls.append((author_id, cursor, page_size))
        return {
            "notes": [{"note_id": note_id, "type": "video"}],
            "cursor": "done",
            "has_more": False,
        }

    adapter = XiaohongshuAuthorDiscoveryAdapter(
        Settings(),
        html_fetcher=lambda _: "<html>risk-trimmed</html>",
        page_fetcher=fetch_page,
    )
    result = asyncio.run(adapter.discover(
        f"https://www.xiaohongshu.com/user/profile/{user_id}",
        scan_limit=1,
    ))

    assert calls == [(user_id, "", 1)]
    assert [work.source_id for work in result.works] == [note_id]
    assert result.extractor_name == "xiaohongshu-signed-user-posted"


def test_xiaohongshu_accepts_fifty_and_rejects_above_configured_limit() -> None:
    adapter = XiaohongshuAuthorDiscoveryAdapter(
        Settings(), html_fetcher=lambda _: "{}"
    )
    with pytest.raises(DiscoveryAdapterError):
        asyncio.run(adapter.discover(
            "https://www.xiaohongshu.com/user/profile/"
            "5c31698d0000000007018a31",
            scan_limit=50,
        ))
    with pytest.raises(ValueError, match="configured range"):
        asyncio.run(adapter.discover(
            "https://www.xiaohongshu.com/user/profile/"
            "5c31698d0000000007018a31",
            scan_limit=51,
        ))


def test_xiaohongshu_default_page_fetcher_signs_json_cookie_request(
    tmp_path,
) -> None:
    cookie_path = tmp_path / "xhs-cookies.json"
    cookie_path.write_text(json.dumps({
        "a1": "a" * 52,
        "webId": "b" * 32,
        "web_session": "session-must-not-leak",
    }))
    cookie_path.chmod(0o600)
    adapter = XiaohongshuAuthorDiscoveryAdapter(Settings(
        xiaohongshu_cookies_file=str(cookie_path),
    ))
    captured = {}

    class Headers:
        @staticmethod
        def get_content_charset():
            return "utf-8"

    class Response:
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        @staticmethod
        def read(_size):
            return json.dumps({
                "success": True,
                "data": {
                    "notes": [],
                    "cursor": "done",
                    "has_more": False,
                },
            }).encode()

    class Opener:
        @staticmethod
        def open(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["timeout"] = timeout
            return Response()

    adapter._opener = Opener()
    result = adapter._download_user_posted_page(
        "5c31698d0000000007018a31", "cursor-1", 20
    )

    assert result["has_more"] is False
    assert "num=20" in captured["url"]
    assert "cursor=cursor-1" in captured["url"]
    assert "user_id=5c31698d0000000007018a31" in captured["url"]
    assert captured["timeout"] == 30
    lower_headers = {
        key.lower(): value for key, value in captured["headers"].items()
    }
    assert {"x-s", "x-s-common", "x-t"} <= set(lower_headers)
    assert "session-must-not-leak" not in repr(captured)


def test_xiaohongshu_cookie_readiness_is_secret_free(tmp_path) -> None:
    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps({"a1": "a" * 52}))
    incomplete.chmod(0o600)
    ready = tmp_path / "ready.json"
    ready.write_text(json.dumps({
        "a1": "a" * 52,
        "web_session": "must-not-be-returned",
    }))
    ready.chmod(0o600)

    assert cookie_auth_status(Settings(
        xiaohongshu_cookies_file=str(tmp_path / "missing.json"),
    )) == "missing"
    assert cookie_auth_status(Settings(
        xiaohongshu_cookies_file=str(incomplete),
    )) == "incomplete"
    assert cookie_auth_status(Settings(
        xiaohongshu_cookies_file=str(ready),
    )) == "ready"


def test_xiaohongshu_rejects_insecure_cookie_permissions(tmp_path) -> None:
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps({
        "a1": "a" * 52,
        "web_session": "must-not-be-returned",
    }))
    cookie_file.chmod(0o644)

    settings = Settings(xiaohongshu_cookies_file=str(cookie_file))
    assert cookie_auth_status(settings) == "incomplete"
    assert not list(xiaohongshu_module._load_cookie_jar(str(cookie_file)))


def test_xiaohongshu_reloads_cookie_file_once_after_atomic_update(
    tmp_path,
) -> None:
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps({
        "a1": "a" * 52,
        "web_session": "first-session",
    }))
    cookie_file.chmod(0o600)
    adapter = XiaohongshuAuthorDiscoveryAdapter(Settings(
        xiaohongshu_cookies_file=str(cookie_file),
    ))
    assert not adapter._reload_cookies_if_changed()

    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps({
        "a1": "b" * 52,
        "web_session": "second-session",
    }))
    replacement.chmod(0o600)
    os.replace(replacement, cookie_file)

    assert adapter._reload_cookies_if_changed()
    assert not adapter._reload_cookies_if_changed()
    loaded = {
        cookie.name: cookie.value for cookie in adapter._cookie_jar
    }
    assert loaded["web_session"] == "second-session"


def test_xiaohongshu_probe_preserves_auth_required_error(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        xiaohongshu_module, "validate_public_url", lambda value: value
    )
    adapter = XiaohongshuAuthorDiscoveryAdapter(
        Settings(
            ytdlp_cookies_file=None,
            xiaohongshu_cookies_file=str(tmp_path / "missing.json"),
        ),
        html_fetcher=lambda _: (
            "<script>window.__INITIAL_STATE__ = "
            "{ user: { notes: [] } };</script>"
        ),
    )
    result = asyncio.run(adapter.probe_access(
        "https://www.xiaohongshu.com/user/profile/"
        "5c31698d0000000007018a31"
    ))
    assert result.status == AccessProbeStatus.AUTH_REQUIRED
    assert result.error_code == PlatformErrorCode.AUTH_EXPIRED.value


def test_xiaohongshu_media_resolution_uses_transient_token_cache(
    monkeypatch,
) -> None:
    adapter = XiaohongshuAuthorDiscoveryAdapter(Settings())
    author_id = "5c31698d0000000007018a31"
    first_id = "6411cf99000000001300b6d9"
    second_id = "674051740000000007027a15"
    page_calls = []
    detail_calls = []

    def page_fetcher(request_author, cursor, page_size):
        page_calls.append((request_author, cursor, page_size))
        return {
            "notes": [
                {
                    "note_id": first_id,
                    "type": "video",
                    "xsec_token": "first-secret",
                },
                {
                    "note_id": second_id,
                    "type": "video",
                    "xsec_token": "second-secret",
                },
            ],
            "cursor": "done",
            "has_more": False,
        }

    def detail_fetcher(request_author, note_id, token):
        detail_calls.append((request_author, note_id, token))
        return {
            "items": [{
                "id": note_id,
                "note_card": {
                    "note_id": note_id,
                    "type": "video",
                    "title": f"title-{note_id[:4]}",
                    "desc": "description",
                    "user": {
                        "user_id": author_id,
                        "nickname": "作者",
                    },
                    "tag_list": [{"name": "测试"}],
                    "video": {
                        "media": {
                            "stream": {
                                "h264": [{
                                    "master_url": (
                                        "http://sns-video-bd.xhscdn.com/"
                                        f"stream/{note_id}?sign=secret"
                                    ),
                                    "avg_bitrate": 1000,
                                    "duration": 123000,
                                }]
                            }
                        }
                    },
                },
            }],
        }

    monkeypatch.setattr(adapter, "_download_user_posted_page", page_fetcher)
    monkeypatch.setattr(adapter, "_download_note_detail", detail_fetcher)
    first = asyncio.run(adapter.resolve_note_media(author_id, first_id))
    second = asyncio.run(adapter.resolve_note_media(author_id, second_id))

    assert len(page_calls) == 1
    assert [call[1] for call in detail_calls] == [first_id, second_id]
    assert [call[2] for call in detail_calls] == [
        "first-secret", "second-secret",
    ]
    assert first["duration"] == pytest.approx(123)
    assert first["author_id"] == author_id
    assert first["media_url"].startswith(
        "https://sns-video-bd.xhscdn.com/"
    )
    assert second["media_url"].startswith(
        "https://sns-video-bd.xhscdn.com/"
    )
    persisted_view = {
        key: value for key, value in second.items() if key != "media_url"
    }
    assert "secret" not in repr(persisted_view)


def test_xiaohongshu_media_resolution_can_reach_items_after_recent_limit(
    monkeypatch,
) -> None:
    adapter = XiaohongshuAuthorDiscoveryAdapter(Settings())
    author_id = "5c31698d0000000007018a31"
    note_ids = [f"{index:024x}" for index in range(1, 4)]
    page_calls = []

    def page_fetcher(request_author, cursor, page_size):
        page = int(cursor or "0")
        page_calls.append((request_author, cursor, page_size))
        return {
            "notes": [{
                "note_id": note_ids[page],
                "type": "video",
                "xsec_token": f"token-{page}",
            }],
            "cursor": str(page + 1),
            "has_more": page < 2,
        }

    def detail_fetcher(request_author, note_id, token):
        return {
            "items": [{
                "id": note_id,
                "note_card": {
                    "note_id": note_id,
                    "type": "video",
                    "title": "较早作品",
                    "user": {
                        "user_id": request_author,
                        "nickname": "作者",
                    },
                    "video": {
                        "media": {
                            "stream": {
                                "h264": [{
                                    "master_url": (
                                        "https://sns-video-bd.xhscdn.com/"
                                        f"stream/{note_id}?sign={token}"
                                    ),
                                    "duration": 1000,
                                }],
                            },
                        },
                    },
                },
            }],
        }

    monkeypatch.setattr(adapter, "_download_user_posted_page", page_fetcher)
    monkeypatch.setattr(adapter, "_download_note_detail", detail_fetcher)

    resolved = asyncio.run(adapter.resolve_note_media(
        author_id,
        note_ids[-1],
        scan_limit=0,
    ))

    assert resolved["id"] == note_ids[-1]
    assert len(page_calls) == 3
    assert all(
        call[2] == xiaohongshu_module.API_PAGE_SIZE
        for call in page_calls
    )


def test_xiaohongshu_share_access_resolves_safe_author_and_cover(
    monkeypatch,
) -> None:
    adapter = XiaohongshuAuthorDiscoveryAdapter(Settings())
    note_id = "674051740000000007027a15"
    author_id = "5c31698d0000000007018a31"
    calls = []

    def detail_fetcher(
        request_author,
        request_note,
        token,
        *,
        xsec_source,
        referer,
    ):
        calls.append((
            request_author,
            request_note,
            token,
            xsec_source,
            referer,
        ))
        return {
            "items": [{
                "note_card": {
                    "note_id": note_id,
                    "type": "video",
                    "title": "菜谱视频",
                    "user": {
                        "user_id": author_id,
                        "nickname": "作者",
                    },
                    "image_list": [{
                        "width": 1080,
                        "height": 1440,
                        "info_list": [{
                            "url": (
                                "http://sns-webpic-qc.xhscdn.com/"
                                "cover.jpg?sign=temporary"
                            ),
                        }],
                    }],
                    "video": {
                        "media": {
                            "stream": {
                                "h264": [{
                                    "master_url": (
                                        "http://sns-video-bd.xhscdn.com/"
                                        "stream/video?sign=temporary"
                                    ),
                                }],
                            },
                        },
                    },
                },
            }],
        }

    monkeypatch.setattr(adapter, "_download_note_detail", detail_fetcher)
    access_url = (
        f"https://www.xiaohongshu.com/explore/{note_id}"
        "?xsec_token=one-time-secret&xsec_source=pc_feed"
    )
    resolved = asyncio.run(
        adapter.resolve_note_access_url(access_url, note_id)
    )

    assert resolved["author_id"] == author_id
    assert resolved["thumbnail_url"].startswith(
        "https://sns-webpic-qc.xhscdn.com/"
    )
    assert calls[0][0] == ""
    assert calls[0][2] == "one-time-secret"
    assert calls[0][3] == "pc_feed"
