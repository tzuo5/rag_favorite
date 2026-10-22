"""Fetch owner-authorized Xiaohongshu notes with a dedicated login profile."""

from __future__ import annotations

import http.client
import http.cookiejar
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .config import ConfigError
from .observation import timed
from .video_provider import ProviderUnavailable
from .video_store import digest_file
from .xhs_private import TrustedRedirect


def validate_source_url(url: str):
    part = urlsplit(url)
    if (
        len(url) > 5000
        or part.scheme != "https"
        or part.username
        or part.password
        or part.port not in {None, 443}
        or part.fragment
        or part.hostname not in {"xhslink.cn", "www.xiaohongshu.com"}
        or not re.fullmatch(
            r"/(?:o/[A-Za-z0-9]+|(?:explore|discovery/item)/[a-f0-9]{24})", part.path
        )
    ):
        raise ConfigError("Only an HTTPS Xiaohongshu share or note URL is supported.")


def source_http_error(status):
    code = {
        401: "XHS_LOGIN_REQUIRED",
        403: "XHS_LOGIN_OR_VERIFICATION_REQUIRED",
        461: "XHS_LOGIN_OR_VERIFICATION_REQUIRED",
        471: "XHS_LOGIN_OR_VERIFICATION_REQUIRED",
        404: "XHS_NOTE_UNAVAILABLE",
    }.get(status, f"XHS_SOURCE_HTTP_{status}")
    return ProviderUnavailable(
        code, status_code=status, retryable=status == 429 or 500 <= status < 600
    )


def check_note_redirect(url):
    """Classify platform landing pages without retaining signed URLs or messages."""
    part = urlsplit(url)
    platform_code = parse_qs(part.query).get("error_code", [""])[0]
    if platform_code in {"300012", "300013", "-100", "-101"}:
        raise ProviderUnavailable(
            "XHS_LOGIN_OR_VERIFICATION_REQUIRED",
            diagnostics={"platform_code": platform_code},
        )
    if part.path.rstrip("/") == "/login":
        raise ProviderUnavailable("XHS_LOGIN_REQUIRED")
    if part.path.rstrip("/") == "/404":
        raise ProviderUnavailable(
            "XHS_NOTE_UNAVAILABLE",
            diagnostics={"platform_code": platform_code}
            if re.fullmatch(r"[0-9-]{1,12}", platform_code)
            else {},
        )


@timed("fetch_source_metadata")
def fetch_note(video, source_url: str) -> dict:
    validate_source_url(source_url)
    from .xhs_session_rpc import SessionRPC

    return SessionRPC(video).call("note", url=source_url)


@timed("download_video")
def download_note_video(
    video, note: dict, *, job_id=None, proxy=None, on_request=None
) -> dict:
    if proxy is None:
        from .xhs_session_rpc import SessionRPC

        return SessionRPC(video).call(
            "download_media",
            note=note,
            job_id=job_id or "note-" + str(note.get("note_id", "")),
            wait=True,
        )["video"]
    from .xhs_proxy import RequiredProxy
    from .xhs_session_policy import classify

    url = note.get("media_url")
    if not url:
        raise ProviderUnavailable("XHS_NOTE_HAS_NO_VIDEO")
    part = urlsplit(url)
    if (
        part.scheme != "https"
        or not part.hostname
        or not part.hostname.endswith(".xhscdn.com")
        or part.username
        or part.password
        or part.port not in {None, 443}
    ):
        raise ConfigError("Unsupported media origin.")
    root = video.root / "assets"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    asset_id = uuid4().hex
    target = root / asset_id
    temporary = root / (".download-" + asset_id)
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Referer": "https://www.xiaohongshu.com/",
                "User-Agent": "Mozilla/5.0",
            },
        )
        opener = urllib.request.build_opener(
            RequiredProxy(proxy, on_request),
            TrustedRedirect(lambda host: host.endswith(".xhscdn.com")),
        )
        with (
            opener.open(req, timeout=60) as response,
            temporary.open("xb") as f,
        ):
            os.fchmod(f.fileno(), 0o600)
            if not (urlsplit(response.url).hostname or "").endswith(".xhscdn.com"):
                raise ConfigError("Media redirect left the trusted CDN.")
            length = response.headers.get("Content-Length")
            expected = int(length) if length is not None else None
            if expected is not None and not 0 < expected <= video.max_asset_bytes:
                raise ConfigError("Invalid or oversized media Content-Length.")
            size = 0
            deadline = time.monotonic() + 600
            for block in iter(lambda: response.read(1_048_576), b""):
                if time.monotonic() > deadline:
                    raise TimeoutError("XHS media deadline")
                size += len(block)
                if size > video.max_asset_bytes:
                    raise ConfigError(
                        "Downloaded media exceeds the configured size limit."
                    )
                f.write(block)
            if not size:
                raise ConfigError("The source returned empty media.")
            if expected is not None and size != expected:
                raise ProviderUnavailable(
                    "Source download ended before the declared byte count.",
                    code="SOURCE_DOWNLOAD_INCOMPLETE",
                    retryable=True,
                    error_category="network",
                )
            f.flush()
            os.fsync(f.fileno())
        validate_download(video, temporary)
        temporary.chmod(0o600)
        temporary.replace(target)
        manifest = {
            "asset_id": asset_id,
            "name": "xhs-" + note["note_id"] + ".mp4",
            "suffix": ".mp4",
            "sha256": digest_file(target),
            "bytes": size,
            "content_length": expected,
            "container_verified": True,
        }
        metadata = root / (asset_id + ".json")
        metadata.write_text(json.dumps(manifest))
        metadata.chmod(0o600)
        return manifest
    except urllib.error.HTTPError as exc:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        error = classify(status=exc.code)
        from .xhs_session_policy import retry_after_seconds

        delay = retry_after_seconds(exc.headers.get("Retry-After"))
        if delay:
            error.diagnostics["retry_after"] = delay
        raise error from None
    except (
        urllib.error.URLError,
        TimeoutError,
        ConnectionError,
        http.client.HTTPException,
    ) as exc:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise ProviderUnavailable(
            "Source media transfer failed.",
            code="XHS_NETWORK_ERROR",
            diagnostics={
                "reason": type(exc).__name__
                + ":"
                + type(getattr(exc, "reason", exc)).__name__
            },
            retryable=True,
            error_category="network",
        ) from exc
    except Exception:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise


@timed("validate_download")
def validate_download(video, path):
    """Demux every packet before admitting media; never pad truncated source data."""
    result = subprocess.run(
        [
            video.ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c",
            "copy",
            "-f",
            "null",
            "-",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=180,
        check=False,
    )
    if result.returncode:
        raise ProviderUnavailable(
            "Downloaded media contains incomplete or corrupt packets.",
            code="SOURCE_DOWNLOAD_INCOMPLETE",
            retryable=True,
            error_category="network",
        )


def normalize_media_url(url):
    """Canonical source identity, independent of topic and signed query tokens."""
    from urllib.parse import parse_qs

    part = urlsplit(url)
    if (
        part.scheme != "https"
        or part.username
        or part.password
        or part.port not in {None, 443}
    ):
        raise ConfigError("Expected an HTTPS video source.")
    host = (part.hostname or "").lower()
    if host in {"www.xiaohongshu.com", "xhslink.cn"}:
        validate_source_url(url)
        match = re.fullmatch(r"/(?:explore|discovery/item)/([a-f0-9]{24})", part.path)
        if match:
            return "xhs:" + match[1], "https://www.xiaohongshu.com/explore/" + match[1]
        return "xhs-share:" + part.path, url
    if host in {"www.youtube.com", "youtube.com", "youtu.be"}:
        ident = (
            part.path.strip("/")
            if host == "youtu.be"
            else parse_qs(part.query).get("v", [""])[0]
        )
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", ident):
            return "youtube:" + ident, "https://www.youtube.com/watch?v=" + ident
    if host in {"www.bilibili.com", "bilibili.com"}:
        match = re.fullmatch(r"/video/(BV[A-Za-z0-9]+|av[0-9]+)/?", part.path)
        if match:
            return "bilibili:" + match[1], "https://www.bilibili.com/video/" + match[1]
    raise ConfigError("Unsupported video source URL.")


def _external_cookie_args(video, key):
    from .video_config import private_environment

    secrets = private_environment(video.credentials_file)
    cookie = secrets.get(
        "BILIBILI_COOKIES_FILE" if key.startswith("bilibili:") else "YTDLP_COOKIES_FILE"
    )
    if not cookie:
        return []
    cookie_path = Path(cookie).expanduser()
    if (
        cookie_path.is_symlink()
        or not cookie_path.is_file()
        or cookie_path.stat().st_mode & 0o077
    ):
        raise ConfigError("Video cookies must be a private regular file.")
    return ["--cookies", str(cookie_path)]


@timed("fetch_external_metadata")
def fetch_external_title(video, source_url):
    """Read the source title before downloading media; keep signed URLs private."""
    import subprocess
    import sys

    key, url = normalize_media_url(source_url)
    if not key.startswith(("youtube:", "bilibili:")):
        raise ConfigError("Not an external video source.")
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--skip-download",
        "--dump-single-json",
        "--socket-timeout",
        "30",
        "--retries",
        "2",
        *_external_cookie_args(video, key),
        url,
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=120, check=False
        )
        info = json.loads(result.stdout) if result.returncode == 0 else {}
        from .title_dedup import title_text

        title = title_text(info.get("title"))
    except (subprocess.TimeoutExpired, ValueError, AttributeError):
        title = ""
    if not title:
        raise ProviderUnavailable(
            "External video title unavailable.", code="EXTERNAL_VIDEO_METADATA_FAILED"
        )
    return title


@timed("download_external_video")
def download_external_video(video, source_url, job_id):
    """yt-dlp source adapter feeding the same staged-asset worker as local/XHS videos."""
    import subprocess
    import sys

    from .video_store import stage_asset

    key, url = normalize_media_url(source_url)
    if not key.startswith(("youtube:", "bilibili:")):
        raise ConfigError("Not an external video source.")
    directory = video.root / "work" / job_id / "download"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = directory / "source.%(ext)s"
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--no-progress",
        "--socket-timeout",
        "30",
        "--retries",
        "2",
        "--max-filesize",
        str(video.max_asset_bytes),
        "--ffmpeg-location",
        video.ffmpeg,
        "--merge-output-format",
        "mp4",
        "--format",
        "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--output",
        str(output),
        "--print",
        "after_move:filepath",
    ]
    command += _external_cookie_args(video, key)
    command += [url]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=1800, check=False
    )
    paths = [
        p
        for p in directory.iterdir()
        if p.suffix in {".mp4", ".mkv", ".webm", ".mov"} and p.is_file()
    ]
    if result.returncode or len(paths) != 1:
        raise ProviderUnavailable(
            "External video download failed.", code="EXTERNAL_VIDEO_DOWNLOAD_FAILED"
        )
    source = paths[0]
    manifest = stage_asset(source, video)
    source.unlink()
    return manifest
