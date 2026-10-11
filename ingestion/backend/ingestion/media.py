from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yt_dlp

from backend.video_processor import (
    VideoProcessor,
    youtube_extractor_args,
    ytdlp_http_headers,
    ytdlp_js_runtimes,
)

from .config import Settings
from .discovery.xiaohongshu import XiaohongshuAuthorDiscoveryAdapter
from .models import Platform, SourceMetadata
from .security import SafeYtdlpLogger, stable_source_url, validate_public_url

logger = logging.getLogger(__name__)


def normalize_platform(extractor: str | None, webpage_url: str | None = None) -> Platform:
    value = (extractor or "").lower()
    if "youtube" in value: return Platform.YOUTUBE
    if "bilibili" in value: return Platform.BILIBILI
    if any(x in value for x in ("xiaohongshu", "xhs")): return Platform.XIAOHONGSHU
    if "tiktok" in value: return Platform.TIKTOK
    if "instagram" in value: return Platform.INSTAGRAM
    if any(x in value for x in ("twitter", "x.com")): return Platform.TWITTER
    return Platform.OTHER


class MetadataExtractor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.xiaohongshu = XiaohongshuAuthorDiscoveryAdapter(settings)

    async def from_url(
        self,
        url: str,
        *,
        author_id: str | None = None,
        author_scan_limit: int | None = None,
    ) -> tuple[SourceMetadata, dict[str, Any]]:
        validate_public_url(url)
        if author_id and (urlsplit(url).hostname or "").lower() in {
            "xiaohongshu.com",
            "www.xiaohongshu.com",
        }:
            note_id = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            resolved = await self.xiaohongshu.resolve_note_media(
                author_id,
                note_id,
                scan_limit=(
                    self.settings.author_discovery_scan_limit
                    if author_scan_limit is None
                    else author_scan_limit
                ),
            )
            duration = resolved.get("duration")
            if (
                duration
                and float(duration) > self.settings.max_video_duration_seconds
            ):
                raise ValueError("视频时长超过配置上限")
            metadata = SourceMetadata(
                platform=Platform.XIAOHONGSHU,
                canonical_url=stable_source_url(url, "xiaohongshu"),
                source_id=note_id,
                original_title=resolved.get("title"),
                author=resolved.get("author"),
                uploader_id=resolved.get("author_id"),
                duration_seconds=duration,
                description=resolved.get("description"),
                thumbnail_url=resolved.get("thumbnail_url"),
                extractor="xiaohongshu-signed-feed",
                extra={"tags": resolved.get("tags") or []},
            )
            return metadata, {
                # Transient worker-only capability; never copy this mapping to
                # job metadata or persistent logs.
                "_transient_media_url": resolved["media_url"],
            }
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 30,
            "extractor_args": youtube_extractor_args(),
            "js_runtimes": ytdlp_js_runtimes(),
            "http_headers": ytdlp_http_headers(),
            "logger": SafeYtdlpLogger(),
        }
        host = (urlsplit(url).hostname or "").lower()
        cookie_file = (
            self.settings.bilibili_cookies_file
            if host == "bilibili.com" or host.endswith(".bilibili.com")
            else self.settings.ytdlp_cookies_file
        )
        if cookie_file:
            opts["cookiefile"] = cookie_file
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, url, False)
        duration = info.get("duration")
        if duration and float(duration) > self.settings.max_video_duration_seconds:
            raise ValueError("视频时长超过配置上限")
        extractor = info.get("extractor_key") or info.get("extractor")
        published = info.get("timestamp") or info.get("release_timestamp")
        if published:
            from datetime import datetime, timezone
            published = datetime.fromtimestamp(published, timezone.utc).isoformat()
        platform = normalize_platform(extractor, info.get("webpage_url"))
        formats = info.get("formats") or [info]
        video_codecs = [item.get("vcodec") for item in formats if isinstance(item, dict)]
        has_video = True if any(v and v != "none" for v in video_codecs) else False if video_codecs and all(v == "none" for v in video_codecs) else None
        metadata = SourceMetadata(
            platform=platform,
            canonical_url=stable_source_url(info.get("webpage_url") or info.get("original_url") or url, platform.value),
            source_id=str(info.get("id")) if info.get("id") is not None else None,
            original_title=info.get("title"),
            author=info.get("uploader") or info.get("channel") or info.get("creator") or info.get("artist"),
            uploader_id=info.get("uploader_id") or info.get("channel_id"),
            published_at=published,
            duration_seconds=duration,
            description=info.get("description"),
            thumbnail_url=info.get("thumbnail"),
            language=info.get("language"),
            extractor=extractor,
            extra={"subtitles": sorted((info.get("subtitles") or {}).keys()), "automatic_captions": sorted((info.get("automatic_captions") or {}).keys()), "has_video": has_video},
        )
        return metadata, info


class TranscriptService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.video_processor = VideoProcessor()
        self._structured_results = {}

    async def url_transcript(self, url: str, job_dir: Path, metadata: SourceMetadata) -> tuple[str, str | None, bool]:
        subtitle, _, language = await self.video_processor.fetch_subtitles(url, job_dir)
        if subtitle:
            return subtitle, language, True
        audio, _ = await self.video_processor.download_and_convert(url, job_dir, metadata.original_title)
        return await self.local_transcript(Path(audio)), None, False

    async def local_transcript(self, media_path: Path, *, control_check=None) -> str:
        result = await self.local_transcript_result(media_path, control_check=control_check)
        markdown = result.to_markdown()
        self._structured_results[hashlib.sha256(markdown.encode()).hexdigest()] = result
        return markdown

    def structured_result(self, markdown: str):
        from .temporal_models import from_markdown
        return self._structured_results.pop(hashlib.sha256(markdown.encode()).hexdigest(), None) or from_markdown(markdown)

    async def local_transcript_result(self, media_path: Path, *, control_check=None):
        if control_check:
            control_check()
        if self.settings.video_asr_backend == "openrouter" and not os.getenv("OPENROUTER_API_KEY"):
            raise ValueError("Missing OpenRouter ASR credential")
        suffix = media_path.suffix.lower()
        audio_path = media_path
        if suffix not in {".wav", ".mp3", ".m4a", ".flac", ".ogg"}:
            audio_path = Path(await self.video_processor.normalize_local_media_to_m4a(media_path, media_path.parent))
        if self.settings.video_asr_backend == "openrouter":
            from .openrouter_asr import OpenRouterASR
            return await OpenRouterASR(self.settings).transcribe(audio_path, control_check=control_check)
        if self.settings.video_asr_backend != "whisper":
            raise ValueError("Unknown ASR backend")
        from backend.transcriber import Transcriber
        return await Transcriber().transcribe_result(str(audio_path))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_duration(path: Path) -> float | None:
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)], capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        return None
    try:
        value = float(json.loads(result.stdout)["format"]["duration"])
        return value if math.isfinite(value) and value >= 0 else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def probe_video_track(path: Path) -> bool | None:
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type", "-of", "json", str(path)], capture_output=True, text=True, timeout=30, check=True)
        return bool(json.loads(result.stdout).get("streams"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


async def download_xiaohongshu_cover(
    url: str | None,
    job_dir: Path,
) -> Path | None:
    """Download and normalize a transient Xiaohongshu cover to local JPEG."""
    if not url:
        return None
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not (
            host == "xhscdn.com"
            or host.endswith(".xhscdn.com")
        )
    ):
        return None
    job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw_path = job_dir / "cover-source"
    cover_path = job_dir / "cover.jpg"

    def _download_and_convert() -> None:
        request = urllib.request.Request(
            url,
            headers={
                **ytdlp_http_headers(),
                "Referer": "https://www.xiaohongshu.com/",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Accept-Encoding": "identity",
            },
        )
        maximum = 12 * 1024 * 1024
        with urllib.request.urlopen(request, timeout=30) as response:
            content_type = str(response.headers.get("Content-Type") or "")
            if content_type and not content_type.lower().startswith("image/"):
                raise ValueError("Xiaohongshu cover response is not an image")
            payload = response.read(maximum + 1)
        if not payload or len(payload) > maximum:
            raise ValueError("Xiaohongshu cover exceeds the size limit")
        with raw_path.open("wb") as handle:
            handle.write(payload)
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
                "-i", str(raw_path), "-frames:v", "1",
                "-vf", "scale='min(1600,iw)':-2",
                "-q:v", "2", str(cover_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        raw_path.unlink(missing_ok=True)
        if result.returncode != 0 or not cover_path.is_file():
            raise RuntimeError("failed to normalize Xiaohongshu cover")

    try:
        await asyncio.to_thread(_download_and_convert)
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        urllib.error.URLError,
    ):
        raw_path.unlink(missing_ok=True)
        cover_path.unlink(missing_ok=True)
        logger.warning("Xiaohongshu cover could not be saved")
        return None
    return cover_path


class CleanupManager:
    def cleanup_media(self, job_dir: Path, keep: set[Path] | None = None) -> None:
        keep = {path.resolve() for path in (keep or set())}
        if not job_dir.exists(): return
        for path in sorted(job_dir.rglob("*"), reverse=True):
            if path.is_file() and path.resolve() not in keep:
                path.unlink(missing_ok=True)
            elif path.is_dir():
                try: path.rmdir()
                except OSError: pass
        try: job_dir.rmdir()
        except OSError: pass

    def purge_expired(self, root: Path, ttl_hours: int) -> int:
        import time
        cutoff = time.time() - ttl_hours * 3600
        count = 0
        for path in root.iterdir() if root.exists() else []:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
                count += 1
        return count
