"""Minimal note parser shared by the lightweight source worker."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, urlunsplit


def dictionaries(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from dictionaries(child)
    elif isinstance(value, list):
        for child in value:
            yield from dictionaries(child)


def preferred_muxed_stream(note: dict):
    streams = [
        s
        for s in dictionaries(note.get("video", {}))
        if (s.get("masterUrl") or s.get("master_url"))
        and float(s.get("audioChannels", s.get("audio_channels", 0)) or 0) > 0
    ]
    reasonable = [s for s in streams if 0 < float(s.get("height", 0) or 0) <= 1080]
    return max(
        reasonable or streams,
        key=lambda s: (
            float(s.get("height", 0) or 0),
            float(s.get("avgBitrate", 0) or 0),
        ),
        default=None,
    )


def balanced_object(text: str, start: int):
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] not in "[{":
        return None
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    quote = None
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'", "`"}:
            quote = char
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def trusted_media(value):
    if not isinstance(value, str) or len(value) > 5000:
        return None
    part = urlsplit(value)
    if (
        part.scheme not in {"http", "https"}
        or not (part.hostname or "").endswith(".xhscdn.com")
        or part.username
        or part.password
        or part.port not in {None, 80, 443}
    ):
        return None
    return urlunsplit(("https", part.hostname, part.path, part.query, ""))


def note_images(note):
    images = note.get("imageList", note.get("image_list", []))
    if not isinstance(images, list) or len(images) > 100:
        raise ValueError("Unexpected image list.")
    result = []
    for index, image in enumerate(images):
        if not isinstance(image, dict):
            raise ValueError("Unexpected image item.")  # noqa: TRY004 - parser contract uses ValueError
        variants = image.get("infoList", image.get("info_list", []))
        full = (
            [
                v.get("url")
                for v in variants
                if isinstance(v, dict)
                and v.get("imageScene", v.get("image_scene")) == "WB_DFT"
            ]
            if isinstance(variants, list)
            else []
        )
        candidates = [
            *full,
            image.get("urlDefault"),
            image.get("url_default"),
            image.get("url"),
            image.get("urlPre"),
            image.get("url_pre"),
        ]
        url = next((u for v in candidates if (u := trusted_media(v))), None)
        live = image.get("livePhoto", image.get("live_photo"))
        live_urls = []
        live_stream = (
            live
            if isinstance(live, dict)
            else image.get("stream", image.get("video", {}))
            if live
            else {}
        )
        if isinstance(live_stream, dict):
            live_urls = [
                u
                for d in dictionaries(live_stream)
                for k, v in d.items()
                if k in {"url", "masterUrl", "master_url", "videoUrl", "video_url"}
                and (u := trusted_media(v))
            ]
        result.append(
            {
                "image_index": index,
                "url": url,
                "live_photo": bool(
                    live or image.get("livePhotoId") or image.get("live_photo_id")
                ),
                "live_video_url": live_urls[0] if live_urls else None,
            }
        )
    return result


def parse_note(html: str, note_id: str):
    from yt_dlp.utils import js_to_json

    match = re.search(r"(?:window\.)?__INITIAL_STATE__\s*=\s*", html)
    literal = balanced_object(html, match.end()) if match else None
    if not literal:
        raise ValueError("Note initialization state is unavailable.")
    state = json.loads(js_to_json(literal))
    candidates = [
        n
        for n in dictionaries(state)
        if str(n.get("noteId") or n.get("note_id") or n.get("id")) == note_id
        and (n.get("title") or n.get("desc") or n.get("video") or n.get("imageList"))
    ]
    if not candidates:
        raise ValueError("The requested note is unavailable.")
    note = max(candidates, key=lambda n: len(n.get("desc", "")))
    description = str(note.get("desc", ""))
    title = str(note.get("title") or "").strip()
    if not title:
        # Titles are optional on Xiaohongshu; a valid note may have only a
        # description or video. Bare ID placeholders are excluded above.
        title = (
            description.strip().splitlines()[0][:120]
            if description.strip()
            else "小红书笔记 " + note_id
        )
    stream = preferred_muxed_stream(note)
    if stream is None:
        streams = [
            s
            for s in dictionaries(note.get("video", {}))
            if s.get("masterUrl") or s.get("master_url")
        ]
        stream = max(
            streams, key=lambda s: float(s.get("avgBitrate", 0) or 0), default=None
        )
    url = (stream.get("masterUrl") or stream.get("master_url")) if stream else None
    if url:
        part = urlsplit(url)
        host = part.hostname or ""
        if (
            part.scheme not in {"http", "https"}
            or not host.endswith(".xhscdn.com")
            or part.username
            or part.password
        ):
            raise ValueError("Unexpected note media origin.")
        url = urlunsplit(("https", part.netloc, part.path, part.query, ""))
    return {
        "note_id": note_id,
        "title": title,
        "description": description,
        "content_type": str(note.get("type", "")),
        "author_id": str(
            note.get("user", {}).get("userId", note.get("user", {}).get("user_id", ""))
        ),
        "published_at": note.get("time"),
        "images": note_images(note),
        "duration": note.get("video", {}).get("capa", {}).get("duration"),
        "media_url": url,
        "stream": {
            k: stream.get(k)
            for k in ("format", "width", "height", "fps", "audioChannels")
        }
        if stream
        else {},
    }
