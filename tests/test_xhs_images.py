from types import SimpleNamespace

import pytest
from PIL import Image

from rag_favorite.video_store import digest_file
from rag_favorite.xhs_images import process_image_note
from rag_favorite.xhs_note import note_images, trusted_media


@pytest.fixture
def note():
    return {
        "note_id": "a" * 24,
        "title": "test",
        "description": "作者正文",
        "images": [
            {"image_index": 0, "url": "https://sns-img.xhscdn.com/one"},
            {"image_index": 1, "url": "https://sns-img.xhscdn.com/two"},
        ],
    }


def download(url, path, **kwargs):
    Image.new("RGB", (16, 16), "white").save(path, "JPEG")
    return {"sha256": digest_file(path), "bytes": path.stat().st_size}


def test_ocr_evidence_has_image_indices_and_no_timestamps(tmp_path, note):
    video = SimpleNamespace(root=tmp_path, visual_enabled=False, max_asset_bytes=100000)
    text, result = process_image_note(
        video,
        note,
        "job",
        "https://www.xiaohongshu.com/explore/" + note["note_id"] + "?xsec_token=secret",
        downloader=download,
        ocr=lambda p: [{"text": "图片文字", "confidence": 0.99, "box": []}],
    )
    assert result["complete"]
    assert "图片文字" in text and "作者正文" in text
    assert note["note_id"] + ":0" in text
    assert "secret" not in text and "start_seconds" not in text
    assert (tmp_path / "derived/job/image-note.json").stat().st_mode & 0o077 == 0


def test_partial_image_failure_retains_success_and_retry_uses_checkpoint(
    tmp_path, note
):
    video = SimpleNamespace(root=tmp_path, visual_enabled=False, max_asset_bytes=100000)

    def failing(url, path, **kwargs):
        if url.endswith("two"):
            raise OSError("download failed")
        return download(url, path, **kwargs)

    _, first = process_image_note(
        video,
        note,
        "job",
        "https://www.xiaohongshu.com/explore/" + note["note_id"],
        downloader=failing,
        ocr=lambda p: [],
    )
    assert not first["complete"] and first["failures"][0]["image_index"] == 1
    downloaded = []

    def retry(url, path, **kwargs):
        downloaded.append(url)
        return download(url, path, **kwargs)

    _, second = process_image_note(
        video,
        note,
        "job",
        "https://www.xiaohongshu.com/explore/" + note["note_id"],
        downloader=retry,
        ocr=lambda p: [],
    )
    assert second["complete"] and len(downloaded) == 1


def test_vision_checkpoint_prevents_repeated_provider_calls(tmp_path, note):
    video = SimpleNamespace(
        root=tmp_path, visual_enabled=True, max_asset_bytes=100000, model="test"
    )
    calls = []
    provider = SimpleNamespace(
        start_visual_budget=lambda: None,
        json=lambda *args: (
            calls.append(args) or {"caption": "visible", "facts": ["supported"]}
        ),
    )
    for _ in range(2):
        text, result = process_image_note(
            video,
            note,
            "job",
            "https://www.xiaohongshu.com/explore/" + note["note_id"],
            downloader=download,
            ocr=lambda p: [],
            provider=provider,
        )
        assert result["complete"] and "supported" in text
    assert len(calls) == 2


def test_image_missing_url_preserves_order_and_live_photo_gap():
    images = note_images(
        {
            "imageList": [
                {"urlDefault": "https://sns-img.xhscdn.com/img", "livePhoto": True},
                {},
            ]
        }
    )
    assert images[0]["live_photo"] and images[1]["image_index"] == 1
    assert images[1]["url"] is None
    assert trusted_media("https://evil.test/img") is None
    assert trusted_media("https://sns-img.xhscdn.com:999/img") is None


def test_live_photo_stream_url_is_retained():
    images = note_images(
        {
            "imageList": [
                {
                    "livePhoto": True,
                    "stream": {
                        "h264": [{"masterUrl": "https://sns-video.xhscdn.com/live.mp4"}]
                    },
                }
            ]
        }
    )
    assert images[0]["live_video_url"] == "https://sns-video.xhscdn.com/live.mp4"
