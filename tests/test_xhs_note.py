import json

import pytest

from rag_favorite.xhs_note import parse_note


def html(note_id, **note):
    return (
        "<script>window.__INITIAL_STATE__="
        + json.dumps(
            {"placeholder": {"id": note_id}, "note": {"noteId": note_id, **note}}
        )
        + ";</script>"
    )


def test_empty_title_video_with_description_is_readable():
    note_id = "a" * 24
    note = parse_note(
        html(
            note_id,
            title="",
            desc="没有标题的视频正文\n第二行",
            type="video",
            video={
                "streams": [
                    {
                        "masterUrl": "https://sns-video-bd.xhscdn.com/video.mp4",
                        "audioChannels": 2,
                    }
                ]
            },
        ),
        note_id,
    )
    assert note["title"] == "没有标题的视频正文"
    assert note["description"] == "没有标题的视频正文\n第二行"
    assert note["media_url"] == "https://sns-video-bd.xhscdn.com/video.mp4"


def test_empty_title_and_description_video_still_has_a_source():
    note_id = "a" * 24
    note = parse_note(
        html(
            note_id,
            title="",
            desc="",
            type="video",
            video={
                "streams": [{"masterUrl": "https://sns-video-bd.xhscdn.com/video.mp4"}]
            },
        ),
        note_id,
    )
    assert note["title"] == "小红书笔记 " + note_id
    assert note["content_type"] == "video" and note["media_url"]


def test_normal_note_without_title_keeps_description():
    note_id = "a" * 24
    note = parse_note(
        html(note_id, title="", desc="无标题的图文知识", type="normal"), note_id
    )
    assert note["description"] == note["title"] == "无标题的图文知识"
    assert note["media_url"] is None


def test_bare_note_placeholder_does_not_claim_readable_content():
    note_id = "a" * 24
    with pytest.raises(ValueError, match="unavailable"):
        parse_note(html(note_id), note_id)


def test_mismatched_note_id_and_untrusted_media_origin_still_rejected():
    note_id = "a" * 24
    with pytest.raises(ValueError, match="unavailable"):
        parse_note(html(note_id, title="视频", type="video"), "b" * 24)
    with pytest.raises(ValueError, match="media origin"):
        parse_note(
            html(
                note_id,
                desc="视频",
                type="video",
                video={"streams": [{"masterUrl": "http://127.0.0.1/private"}]},
            ),
            note_id,
        )
