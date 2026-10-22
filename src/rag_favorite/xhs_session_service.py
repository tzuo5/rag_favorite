"""One long-lived Chrome context for all owner-authorized Xiaohongshu reads.

No polling of the platform while idle. The local login loop runs only during an
explicit ten-minute interaction; normal traffic itself supplies health evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import signal
import socket
import struct
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .video_provider import ProviderUnavailable
from .xhs_private import atomic_private_json, browser_session_lock, private_json
from .xhs_session_policy import (
    RISK_CODES,
    SessionPolicy,
    classify,
    failure,
    retry_after_seconds,
)

NOTE_ID = re.compile(r"^[a-f0-9]{24}$")
READS = {"owner", "favorites", "following", "author", "note"}
RECOVERABLE = [
    "XHS_LOGIN_REQUIRED",
    "XHS_LOGIN_OR_VERIFICATION_REQUIRED",
    "XHS_AUTH_REQUIRED",
    "XHS_VERIFICATION_REQUIRED",
    "XHS_SESSION_UNKNOWN",
    "XHS_PLATFORM_BLOCKED",
    "XHS_RATE_LIMITED",
]
QR_SELECTORS = (
    "[class*='qrcode'] canvas",
    "[class*='qrcode'] img",
    "[class*='qr-code'] canvas",
    "[class*='qr-code'] img",
    ".qrcode-img",
)
CHALLENGE_SELECTORS = (
    "[class*='captcha']:visible",
    "[class*='verify-container']:visible",
    "iframe[src*='captcha']:visible",
    "iframe[title*='验证']:visible",
)
LOGIN_SELECTORS = (
    "[class*='login-container']:visible",
    "[class*='login-modal']:visible",
    "[class*='login-box']:visible",
)


def export_cookies(root, cookies):
    """Publish a derived Netscape jar, retaining session-cookie semantics."""
    jar = http.cookiejar.MozillaCookieJar()
    for item in cookies:
        domain = item["domain"]
        if domain.lstrip(".") != "xiaohongshu.com" and not domain.endswith(
            ".xiaohongshu.com"
        ):
            continue
        expires = item.get("expires", -1)
        jar.set_cookie(
            http.cookiejar.Cookie(
                0,
                item["name"],
                item["value"],
                None,
                False,
                domain,
                domain.startswith("."),
                domain.startswith("."),
                item["path"],
                True,
                item.get("secure", False),
                int(expires) if expires > 0 else None,
                expires <= 0,
                None,
                None,
                {"HttpOnly": None} if item.get("httpOnly") else {},
                False,
            )
        )
    fd, name = tempfile.mkstemp(prefix=".cookies-", dir=root)
    os.close(fd)
    try:
        jar.save(name, ignore_discard=True, ignore_expires=False)
        with open(name, "rb") as stream:
            os.fsync(stream.fileno())
        os.replace(name, root / "cookies.txt")
    finally:
        Path(name).unlink(missing_ok=True)


def decode_qr(payload):
    try:
        import cv2
        import numpy as np

        value, _, _ = cv2.QRCodeDetector().detectAndDecode(
            cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        )
        # Decoded platform URI is delivered only to the authenticated owner.
        part = urlsplit(value)
        if part.scheme == "xhsdiscover" or (
            part.scheme == "https"
            and (
                part.hostname == "xiaohongshu.com"
                or (part.hostname or "").endswith(".xiaohongshu.com")
            )
        ):
            return value
    except Exception:  # noqa: BLE001 - QR library diagnostics may contain private input
        return None
    return None


class DatabaseMirror:
    def __init__(self, config, video):
        self.config, self.video = config, video

    def publish(self, policy):
        from psycopg.types.json import Jsonb

        from .database import connect_database
        from .video_store import library_id

        blocked, until, auth = policy.download_gate()
        with connect_database(self.config) as connection:
            connection.execute(
                "INSERT INTO public.rag_video_pipeline_control "
                "(library_id,xhs_session_state,xhs_session_version,xhs_download_blocked,xhs_login_required,xhs_circuit_until) "
                "VALUES(%s,%s,%s,%s,%s,to_timestamp(%s)) ON CONFLICT(library_id) DO UPDATE SET "
                "xhs_session_state=excluded.xhs_session_state,xhs_session_version=excluded.xhs_session_version,"
                "xhs_download_blocked=excluded.xhs_download_blocked,xhs_login_required=excluded.xhs_login_required,"
                "xhs_circuit_until=excluded.xhs_circuit_until,updated_at=now() "
                "WHERE rag_video_pipeline_control.xhs_session_version<=excluded.xhs_session_version",
                (
                    library_id(self.video),
                    Jsonb(policy.public()),
                    policy.data["version"],
                    blocked,
                    auth,
                    until,
                ),
            )

    def seed(self, policy):
        from .database import connect_database
        from .video_store import library_id

        with connect_database(self.config) as connection:
            row = connection.execute(
                "SELECT xhs_session_state,xhs_session_version FROM public.rag_video_pipeline_control WHERE library_id=%s",
                (library_id(self.video),),
            ).fetchone()
        if row and row[1] >= policy.data["version"]:
            policy.data["version"] = row[1]
            policy.data["paths"].update(row[0].get("paths", {}))
            policy.save()

    def candidate(self):
        from .database import connect_database
        from .video_store import library_id

        with connect_database(self.config) as connection:
            row = connection.execute(
                "SELECT payload->>'source_url' FROM public.rag_video_jobs "
                "WHERE library_id=%s AND state IN ('blocked','running','queued') "
                "AND (state<>'blocked' OR error_code=ANY(%s)) "
                "AND coalesce(payload->>'asset_id','')='' "
                "AND payload->>'source_url' ~ '^https://(www[.]xiaohongshu[.]com|xhslink[.]cn)/' "
                "ORDER BY (state='blocked') DESC,updated_at DESC LIMIT 1",
                (library_id(self.video), RECOVERABLE),
            ).fetchone()
        return row[0] if row else None

    def source_for_job(self, job_id):
        from uuid import UUID

        from .database import connect_database
        from .video_store import library_id

        try:
            ident = str(UUID(str(job_id)))
        except ValueError:
            return None
        with connect_database(self.config) as connection:
            row = connection.execute(
                "SELECT payload->>'source_url' FROM rag_video_jobs WHERE library_id=%s AND id=%s",
                (library_id(self.video), ident),
            ).fetchone()
        return row[0] if row else None

    def recovered(self, policy):
        """Only session-owned failures, never manual pauses or unrelated blocks."""
        if policy.data["recovery"]["phase"] not in {"observation", "steady"}:
            raise failure("XHS_ACCEPTANCE_PAUSED", delay=60)
        from .database import connect_database
        from .video_store import library_id

        codes = RECOVERABLE
        with connect_database(self.config) as connection:
            rows = connection.execute(
                "UPDATE public.rag_video_pipeline p SET status='queued',error_code=NULL,retry_at=now(),updated_at=now() "
                "FROM public.rag_video_jobs j WHERE p.job_id=j.id AND p.library_id=%s AND p.stage='download' "
                "AND p.status='blocked' AND p.error_code=ANY(%s) AND j.error_code=ANY(%s) "
                "AND coalesce(j.payload->>'asset_id','')='' "
                "AND j.payload->>'source_url' ~ '^https://(www[.]xiaohongshu[.]com|xhslink[.]cn)/' RETURNING p.job_id",
                (library_id(self.video), codes, codes),
            ).fetchall()
            if rows:
                connection.execute(
                    "UPDATE public.rag_video_jobs SET state='queued',stage='download',error_code=NULL,updated_at=now() "
                    "WHERE id=ANY(%s::uuid[])",
                    ([str(r[0]) for r in rows],),
                )


class SessionService:
    def __init__(self, video, *, mirror=None, policy=None):
        self.video = video
        self.root = video.root.parent / "xhs-session"
        self.policy = policy or SessionPolicy(self.root)
        self.proxy = None
        if getattr(video, "xhs_proxy_config", None):
            from .xhs_proxy import ProxyPool

            self.proxy = ProxyPool(self.root, video.xhs_proxy_config)
            self.proxy.reconcile(video.root)
            if self.policy.data["recovery"]["phase"] == "initial":
                self.policy.pause("XHS_ACCEPTANCE_PAUSED")
        self.mirror = mirror
        self.lock = asyncio.Lock()
        self.context = self.page = self.signer = self.sign_session = None
        self.owner_id, self.owner_at = None, 0
        self.login = None
        self.frame = None
        self.note_cache = {}
        self.diagnostic = False
        self.network_active = False

    async def publish(self):
        if self.proxy:
            self.policy.data["network"] = self.proxy.status()
        if self.mirror:
            await asyncio.to_thread(self.mirror.publish, self.policy)

    async def reserve_extra(self, operation):
        # A click that explicitly starts another API request is another operation.
        # Keep the compound operation serial, but never wait out an hourly quota.
        delay = self.policy.budget()["next_operation_at"] - self.policy.clock()
        if delay > 61:
            raise failure("XHS_DEFERRED", delay=delay + 1)
        if delay > 0:
            await asyncio.sleep(delay)
        self.policy.reserve(
            operation, interactive=self.diagnostic or self.active_login()
        )
        await self.publish()

    async def start(self, playwright):
        from xhshow import SessionManager, Xhshow
        from xhshow.config import CryptoConfig

        chrome = shutil.which("google-chrome")
        if not chrome:
            raise failure("XHS_SOURCE_BROWSER_MISSING")
        if self.mirror:
            await asyncio.to_thread(self.mirror.seed, self.policy)
        self.context = await playwright.chromium.launch_persistent_context(
            str(self.root / "profile"),
            executable_path=chrome,
            headless=True,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
            args=["--disable-dev-shm-usage"],
            **({"proxy": {"server": self.proxy.url}} if self.proxy else {}),
        )
        self.page = (
            self.context.pages[0]
            if self.context.pages
            else await self.context.new_page()
        )
        for page in self.context.pages[1:]:
            await page.close()

        # Top-level navigation and popups are constrained; normal resource loads
        # remain controlled by the platform's page, not an arbitrary proxy API.
        async def route(request_route):
            req = request_route.request
            if not self.network_active and not self.active_login():
                await request_route.abort()
                return
            self.policy.count(
                "browser_"
                + (
                    "page_requests"
                    if req.is_navigation_request()
                    else "resource_requests"
                )
            )
            self.policy.save()
            if req.is_navigation_request() and req.frame == self.page.main_frame:
                part = urlsplit(req.url)
                host = part.hostname or ""
                if part.scheme != "https" or not (
                    host == "xhslink.cn"
                    or host == "xiaohongshu.com"
                    or host.endswith(".xiaohongshu.com")
                ):
                    await request_route.abort()
                    return
            await request_route.continue_()

        await self.context.route("**/*", route)
        self.context.on(
            "page",
            lambda page: (
                asyncio.create_task(page.close()) if page != self.page else None
            ),
        )
        self.ua = await self.page.evaluate("navigator.userAgent")
        signing = CryptoConfig(PUBLIC_USERAGENT=self.ua)
        self.signer, self.sign_session = Xhshow(signing), SessionManager(signing)
        if self.proxy:
            await self.page.goto("about:blank")
        # Never import a stale cookie export over the persistent profile.
        await self.export()
        await self.publish()

    async def export(self):
        export_cookies(self.root, await self.context.cookies())
        atomic_private_json(
            self.root / "status.json",
            {
                "state": "cookies_exported" if self.owner_id else "session_managed",
                "managed": True,
                "generation": self.policy.data["version"],
            },
        )

    def status(self):
        login = self.login
        return {
            **self.policy.public(),
            "service": "ready",
            "limits": {
                key: self.policy.budget()[key]
                for key in (
                    "interval_seconds",
                    "operations_per_hour",
                    "notes_per_hour",
                    "notes_per_24h",
                )
            },
            "network": self.proxy.status() if self.proxy else None,
            "route": self.proxy.route_mode if self.proxy else "unconfigured",
            "diagnostics": self.policy.data.get("diagnostics", {}),
            "checked_at": max(
                (p.get("checked_at", 0) for p in self.policy.data["paths"].values()),
                default=None,
            ),
            "pause_reason": self.policy.data["recovery"].get("reason"),
            "screenshot_notice": "截图刷新不等于重新验证",
            "login": {
                key: login.get(key)
                for key in ("id", "state", "expires_at", "error_code")
            }
            if login
            else None,
        }

    def observe(self):
        """Local hourly samples, including Chrome's cgroup, without site traffic."""
        record = {"at": time.time(), **self.policy.public()}
        try:
            relative = Path("/proc/self/cgroup").read_text().split("0::", 1)[1].strip()
            group = Path("/sys/fs/cgroup") / relative.lstrip("/")
            record["memory_bytes"] = int((group / "memory.current").read_text())
            cpu = dict(
                line.split() for line in (group / "cpu.stat").read_text().splitlines()
            )
            record["cpu_usage_usec"] = int(cpu.get("usage_usec", 0))
        except (OSError, IndexError, ValueError):
            record["memory_bytes"] = None
        with (self.root / "observations.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
        (self.root / "observations.jsonl").chmod(0o600)

    async def evidence(self):
        text = (await self.page.locator("body").inner_text(timeout=3000))[:24000]
        if "300012" in text or "IP存在风险" in text or "IP 存在风险" in text:
            return "platform_risk"
        if any(
            [
                await self.page.locator(selector).count()
                for selector in CHALLENGE_SELECTORS
            ]
        ):
            return "verification"
        if any(
            word in text for word in ("拖动滑块", "完成验证", "安全验证", "点击下图")
        ):
            return "verification"
        if any(word in text for word in ("笔记已删除", "笔记不存在", "该内容无法展示")):
            return "note_unavailable"
        login = self.page.locator(", ".join(LOGIN_SELECTORS))
        visible = await login.count() and await login.first.is_visible()
        return "login" if visible or urlsplit(self.page.url).path == "/login" else None

    async def signed_get(self, uri, params):
        url = self.signer.build_url("https://edith.xiaohongshu.com" + uri, params)
        cookies = await self.context.cookies([url])
        current = {
            c["name"]: c["value"] for c in sorted(cookies, key=lambda c: len(c["path"]))
        }
        headers = self.signer.sign_headers_get(
            uri, current, params=params, session=self.sign_session
        )
        headers.update(
            {
                "Origin": "https://www.xiaohongshu.com",
                "Referer": "https://www.xiaohongshu.com/",
                "User-Agent": self.ua,
                "Accept": "application/json",
            }
        )
        self.policy.count("api_http_requests")
        self.policy.save()
        response = await self.context.request.get(
            url, headers=headers, timeout=30000, max_redirects=0
        )
        try:
            if response.status != 200:
                error = classify(status=response.status)
                delay = retry_after_seconds(response.headers.get("retry-after"))
                if delay:
                    error.diagnostics["retry_after"] = delay
                raise error
            raw = await response.body()
            if len(raw) > 8_000_000:
                raise failure("XHS_SESSION_UNKNOWN")
            body = json.loads(raw)
            if not isinstance(body, dict) or body.get("success") is not True:
                evidence = (
                    await self.evidence() if self.page.url != "about:blank" else None
                )
                raise classify(
                    platform=body.get("code") if isinstance(body, dict) else None,
                    evidence=evidence
                    if evidence in {"verification", "platform_risk"}
                    else None,
                )
            if not isinstance(body.get("data"), dict):
                raise failure("XHS_SESSION_UNKNOWN")
            return body["data"]
        finally:
            await response.dispose()

    async def owner(self):
        data = await self.signed_get("/api/sns/web/v2/user/me", {})
        owner = data.get("user_id") or data.get("userId")
        if (
            data.get("guest") is True
            or not isinstance(owner, str)
            or not NOTE_ID.fullmatch(owner)
        ):
            # A guest response plus visible login UI is required for auth_expired.
            evidence = await self.evidence() if self.page.url != "about:blank" else None
            raise classify(
                evidence="login_and_guest" if evidence == "login" else evidence
            )
        binding_path = self.video.root / "favorites" / "owner.json"
        binding = private_json(binding_path) if binding_path.exists() else {}
        if binding.get("owner_hash") not in {
            None,
            hashlib.sha256(owner.encode()).hexdigest(),
        }:
            self.owner_id = None
            raise failure("XHS_ACCOUNT_MISMATCH")
        self.owner_id, self.owner_at = owner, time.time()
        return owner

    async def note(self, url):
        from .video_sources import validate_source_url
        from .xhs_note import parse_note

        validate_source_url(url)
        response = await self.page.goto(
            url, wait_until="domcontentloaded", timeout=45000
        )
        part = urlsplit(self.page.url)
        platform = parse_qs(part.query).get("error_code", [None])[0]
        evidence = await self.evidence()
        if part.path.rstrip("/") == "/404" and platform not in {
            "300012",
            "300013",
            "-100",
            "-101",
        }:
            raise classify(evidence="note_unavailable", platform=platform)
        if (
            platform
            or evidence in {"verification", "note_unavailable", "platform_risk"}
            or (response and response.status >= 400)
        ):
            raise classify(
                status=response.status if response else None,
                platform=platform,
                evidence=evidence,
            )
        match = re.fullmatch(r"/(?:explore|discovery/item)/([a-f0-9]{24})", part.path)
        if not match:
            raise classify(evidence=evidence)
        try:
            return parse_note(await self.page.content(), match[1])
        except (ValueError, KeyError) as exc:
            unavailable = str(exc) == "The requested note is unavailable."
            error = classify(
                status=response.status if response else None,
                evidence="note_unavailable" if unavailable else evidence,
            )
            error.diagnostics["reason"] = (
                "note_absent" if unavailable else "note_parse_failed"
            )
            raise error from None

    async def following(self, owner):
        from .xhs_following_client import FOLLOWING_ALL, normalize_following

        captured = []

        async def capture(response):
            part = urlsplit(response.url)
            if part.hostname != "edith.xiaohongshu.com" or part.path != FOLLOWING_ALL:
                return
            try:
                raw = await response.body()
                if len(raw) <= 8_000_000:
                    body = json.loads(raw)
                    if body.get("success") is True:
                        captured.append(body["data"])
            except Exception:  # noqa: BLE001 - browser response diagnostics are private
                return

        self.page.on("response", capture)
        try:
            await self.page.goto(
                "https://www.xiaohongshu.com/user/profile/" + owner,
                wait_until="domcontentloaded",
                timeout=45000,
            )
            await self.page.wait_for_timeout(3000)
            evidence = await self.evidence()
            if evidence:
                raise classify(evidence=evidence)
            counter = self.page.locator(".user-interactions").first
            text = await counter.locator(":scope > div").first.inner_text(timeout=3000)
            match = re.fullmatch(r"\s*([0-9,]+)\s*关注\s*", text)
            if not match:
                raise failure("XHS_SESSION_UNKNOWN")
            if not captured:
                await self.reserve_extra("following")
                await self.page.get_by_text("关注", exact=True).first.click(
                    timeout=3000
                )
                await self.page.wait_for_timeout(3000)
            if not captured:
                raise failure("XHS_SESSION_UNKNOWN")
            count = int(match[1].replace(",", ""))
            return {
                "authors": normalize_following(captured[-1], count),
                "complete": True,
                "count": count,
                "evidence": "following_all_matches_exact_profile_count",
            }
        finally:
            self.page.remove_listener("response", capture)

    async def read(self, operation, params, *, interactive=False):
        self.policy.check(operation, interactive=interactive)
        self.check_acceptance(operation, params, interactive=interactive)
        if self.proxy and operation == "note" and not interactive:
            self.proxy.before_download()
        await self.ensure_network(diagnostic=interactive)
        if (
            operation == "owner"
            and not interactive
            and self.owner_id
            and time.time() - self.owner_at < 300
        ):
            return self.owner_id
        if operation != "owner" and not self.owner_id:
            # Caller retries after the shared account pacing interval.
            await self.read("owner", {}, interactive=interactive)
        if (
            self.proxy
            and operation not in {"owner", "note"}
            and not interactive
            and self.proxy.data["node_validated_generation"]
            != self.proxy.data["generation"]
        ):
            candidate = (
                await asyncio.to_thread(self.mirror.candidate) if self.mirror else None
            )
            if not candidate:
                raise failure("XHS_SOURCE_URL_REQUIRED")
            await self.read("note", {"url": candidate})
        self.policy.reserve(operation, interactive=interactive)
        await self.publish()
        self.network_active = True
        try:
            if operation == "owner":
                result = await self.owner()
            elif operation == "note":
                result = await self.note(params["url"])
            elif operation == "following":
                result = await self.following(self.owner_id)
            elif operation == "favorites":
                result = await self.signed_get(
                    "/api/sns/web/v2/note/collect/page",
                    {
                        "user_id": self.owner_id,
                        "num": 30,
                        "cursor": params.get("cursor", ""),
                    },
                )
            else:
                result = await self.signed_get(
                    "/api/sns/web/v1/user_posted",
                    {
                        "user_id": params["author_id"],
                        "num": 30,
                        "cursor": params.get("cursor", ""),
                        "image_formats": "jpg,webp,avif",
                        "xsec_source": "pc_user",
                    },
                )
            self.policy.success(operation)
            await self.publish()
            if operation == "note" and self.proxy and not self.diagnostic:
                self.proxy.data["node_validated_generation"] = self.proxy.data[
                    "generation"
                ]
                self.proxy.save()
                self.policy.data.setdefault("note_permits", {})[result["note_id"]] = {
                    "at": self.policy.clock(),
                    "generation": self.proxy.data["generation"],
                }
                self.policy.save()
            return result
        except ProviderUnavailable as exc:
            path = (
                "owner"
                if exc.code in {"XHS_AUTH_REQUIRED", "XHS_ACCOUNT_MISMATCH"}
                else operation
            )
            self.network_failure(path, exc, diagnostic=interactive)
            await self.publish()
            raise
        except Exception as exc:  # noqa: BLE001 - never expose signed URLs or browser errors
            from playwright.async_api import Error as BrowserError
            from playwright.async_api import TimeoutError as BrowserTimeout

            error = failure(
                "XHS_NETWORK_ERROR"
                if isinstance(exc, (BrowserTimeout, BrowserError, ConnectionError))
                else "XHS_SESSION_UNKNOWN"
            )
            network_code = re.search(r"net::ERR_[A-Z_]+", str(exc))
            error.diagnostics["reason"] = type(exc).__name__ + (
                ":" + network_code.group(0) if network_code else ""
            )
            self.network_failure(operation, error, diagnostic=interactive)
            await self.publish()
            raise error from None
        finally:
            self.network_active = False
            await self.export()
            if (
                self.proxy
                and not self.diagnostic
                and not self.active_login()
                and self.page.url != "about:blank"
            ):
                await self.page.goto("about:blank")

    async def ensure_network(self, *, diagnostic=False):
        if self.proxy:
            if (
                self.proxy.running_generation != self.proxy.data["generation"]
                and self.page
                and self.page.url != "about:blank"
            ):
                await self.page.goto("about:blank")
            changed = (
                await asyncio.to_thread(self.proxy.ensure_started, diagnostic=True)
                if self.diagnostic or diagnostic
                else await asyncio.to_thread(self.proxy.ensure_started)
            )
            if changed:
                self.owner_id, self.owner_at = None, 0
                self.note_cache.clear()
                await self.publish()

    def network_failure(self, path, exc, *, diagnostic=False):
        if (
            self.proxy
            and exc.code == "XHS_NETWORK_ERROR"
            and not (self.diagnostic or diagnostic)
        ):
            self.proxy.failed(exc.code)
            self.policy.count(exc.code + "_node_failures")
            if self.proxy.data["paused"]:
                self.policy.pause("XHS_PROXY_PAUSED")
        if self.proxy and exc.code in RISK_CODES:
            self.proxy.data.update(paused=True, reason=exc.code)
            self.proxy.save()
        self.policy.failed(path, exc)

    def check_acceptance(self, operation, params, *, interactive=False):
        if interactive:
            return
        recovery = self.policy.data["recovery"]
        if recovery["phase"] != "acceptance":
            return
        allowed = recovery.get("allowed_notes", [])
        if operation == "note":
            from .xhs_private import note_identity

            if note_identity(params.get("url", "")) not in allowed:
                raise failure("XHS_ACCEPTANCE_PAUSED", delay=60)
        if (
            operation == "media"
            and params.get("job_id") not in recovery["allowed_jobs"]
        ):
            raise failure("XHS_ACCEPTANCE_PAUSED", delay=60)

    async def download_media(self, params):
        from .xhs_media_service import MediaBundle

        if not self.proxy:
            raise failure("XHS_PROXY_UNAVAILABLE", delay=30)
        bundle = MediaBundle(self.video, params["note"], params["job_id"])
        if await asyncio.to_thread(bundle.ready):
            # A lost RPC reply or restart never downloads or counts the same note twice.
            if bundle.data.get("downloaded"):
                self.proxy.complete(bundle.data["note_id"], bundle.data["generation"])
            self.record_download(params, bundle.data)
            return bundle.data
        self.check_acceptance("media", params)
        if self.active_login():
            raise failure("XHS_DEFERRED", delay=30)
        self.policy.check("media")
        if self.proxy.route_mode != "pool":
            raise failure("XHS_ACCEPTANCE_PAUSED", delay=60)
        self.proxy.before_download()
        await self.ensure_network()
        await self.read("owner", {})
        if (
            self.proxy.data["node_validated_generation"]
            != self.proxy.data["generation"]
        ):
            url = (
                await asyncio.to_thread(self.mirror.source_for_job, params["job_id"])
                if self.mirror
                else params["note"].get("source_url")
            )
            if not url:
                raise failure("XHS_SOURCE_URL_REQUIRED")
            await self.read("note", {"url": url})
        self.policy.reserve("media")
        self.policy.reserve_media_note(
            params["note"]["note_id"], self.proxy.data["generation"]
        )
        await self.publish()
        task = asyncio.create_task(
            asyncio.to_thread(
                bundle.download, self.proxy.url, self.proxy.data["generation"]
            )
        )
        try:
            result = await asyncio.shield(task)
            if result["downloaded"]:
                self.proxy.complete(result["note_id"], result["generation"])
            self.record_download(params, result)
            self.policy.success("media")
            return result
        except asyncio.CancelledError:
            # Never release the route lock while a transfer thread is still alive.
            try:
                result = await task
            except ProviderUnavailable as exc:
                self.network_failure("media", exc)
                raise
            if result["downloaded"]:
                self.proxy.complete(result["note_id"], result["generation"])
            self.record_download(params, result)
            self.policy.success("media")
            raise
        except ProviderUnavailable as exc:
            self.network_failure("media", exc)
            raise
        finally:
            self.policy.data.setdefault("media_request_counts", {})[
                params["job_id"]
            ] = bundle.data.get("media_http_requests", 0)
            self.policy.data.setdefault("counters", {})["media_http_requests"] = sum(
                self.policy.data["media_request_counts"].values()
            )
            self.policy.save()
            await self.publish()

    def record_download(self, params, result):
        from .xhs_acceptance import media_kind

        recovery = self.policy.data["recovery"]
        if (
            recovery["phase"] == "acceptance"
            and params["job_id"] not in recovery["allowed_jobs"]
        ):
            return
        if (
            recovery["phase"] == "observation"
            and result.get("completed_at", 0) < recovery["observation_started_at"]
        ):
            return
        if recovery["phase"] in {"acceptance", "observation"}:
            recovery.setdefault("downloads", {})[params["job_id"]] = {
                "note_id": result["note_id"],
                "at": self.policy.clock(),
                "generation": result["generation"],
                "media_validated": True,
                "kind": media_kind(params["note"]),
            }
            self.policy.save()

    def active_login(self):
        return (
            self.login
            and self.login["state"] in {"waiting", "verifying"}
            and self.login["expires_at"] > time.time()
        )

    def check_attempt(self, params, *, frame=False):
        if not self.active_login() or params.get("attempt_id") != self.login["id"]:
            raise failure("XHS_INTERACTION_EXPIRED")
        if frame and (
            not self.frame
            or params.get("frame_id") != self.frame["id"]
            or time.time() - self.frame["at"] > 30
        ):
            raise failure("XHS_INTERACTION_EXPIRED")

    async def begin_login(self):
        if self.active_login():
            return self.status()
        # One explicit navigation, charged before it can reach the platform.
        self.policy.reserve("login", interactive=True)
        self.policy.pause("XHS_ACCEPTANCE_PAUSED")
        route = self.proxy.route_mode if self.proxy else "pool"
        records = self.policy.data.setdefault("diagnostics", {})
        if route in records:
            self.archive_diagnostic(route)
        self.login = {
            "id": str(uuid4()),
            "state": "waiting",
            "expires_at": time.time() + 600,
            "route": route,
        }
        self.frame = None
        self.note_cache.clear()
        self.policy.count("login_interactions")
        self.policy.save()
        # A challenge already on screen must not be discarded for a new QR.
        if self.page.url == "about:blank" or await self.evidence() != "verification":
            await self.page.goto(
                "https://www.xiaohongshu.com/explore",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            if await self.stop_risky_login():
                return self.status()
            for selector in (
                "button:has-text('登录')",
                "[class*='login-btn']",
                "[class*='login-button']",
            ):
                locator = self.page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    await self.reserve_extra("login")
                    await locator.click(timeout=3000)
                    break
            await self.stop_risky_login()
        return self.status()

    async def stop_risky_login(self):
        if await self.evidence() != "platform_risk":
            return False
        exc = classify(platform="300012")
        self.network_failure("login", exc, diagnostic=True)
        self.login.update(state="blocked", error_code=exc.code)
        await self.page.screenshot(
            path=str(self.root / "login-platform-risk.jpg"), type="jpeg", quality=75
        )
        self.policy.data["paths"]["login"]["screenshot_at"] = self.policy.clock()
        self.policy.save()
        await self.page.goto("about:blank")
        await self.publish()
        return True

    async def login_frame(self, params):
        self.check_attempt(params)
        picture = await self.page.screenshot(type="jpeg", quality=75)
        qr, link = None, None
        for selector in QR_SELECTORS:
            locator = self.page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                qr = await locator.screenshot(type="png")
                link = await asyncio.to_thread(decode_qr, qr)
                break
        region = None
        for selector in (*CHALLENGE_SELECTORS, *LOGIN_SELECTORS):
            locator = self.page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                region = await locator.bounding_box()
                if region:
                    break
        self.frame = {"id": str(uuid4()), "at": time.time(), "region": region}
        return {
            "frame_id": self.frame["id"],
            "attempt_id": self.login["id"],
            "width": 1280,
            "height": 900,
            "image": base64.b64encode(picture).decode(),
            "qr": base64.b64encode(qr).decode() if qr else None,
            "qr_link": link,
            "captured_at": self.frame["at"],
            "checked_at": self.status()["checked_at"],
            "notice": "截图刷新不等于重新验证",
        }

    async def interact(self, operation, params):
        self.check_attempt(params, frame=True)
        host = urlsplit(self.page.url).hostname or ""
        if not (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")):
            raise failure("XHS_INTERACTION_INVALID")
        # Prevent using the login view as a general account browser once complete.
        if self.login["state"] != "waiting":
            raise failure("XHS_INTERACTION_EXPIRED")
        if await self.evidence() not in {"login", "verification"}:
            raise failure("XHS_INTERACTION_INVALID")
        if operation == "otp":
            code = params.get("code", "")
            if not re.fullmatch(r"[0-9]{4,8}", code):
                raise failure("XHS_INTERACTION_INVALID")
            locator = self.page.locator(
                "input[placeholder*='验证码'], input[autocomplete='one-time-code']"
            ).first
            await locator.fill(code, timeout=3000)
            params.pop("code", None)
            del code
            for label in ("验证", "确认", "登录"):
                button = self.page.get_by_role("button", name=label, exact=True)
                if (
                    await button.count() == 1
                    and await button.is_visible()
                    and await button.is_enabled()
                ):
                    await button.click(timeout=3000)
                    break
        else:
            points = params.get("points")
            if not isinstance(points, list) or not 1 <= len(points) <= 80:
                raise failure("XHS_INTERACTION_INVALID")
            for p in points:
                if (
                    not isinstance(p, list)
                    or len(p) != 2
                    or not all(type(v) in {int, float} for v in p)
                    or not (0 <= p[0] < 1280 and 0 <= p[1] < 900)
                ):
                    raise failure("XHS_INTERACTION_INVALID")
            region = self.frame.get("region")
            if not region or any(
                not (
                    region["x"] <= p[0] <= region["x"] + region["width"]
                    and region["y"] <= p[1] <= region["y"] + region["height"]
                )
                for p in points
            ):
                raise failure("XHS_INTERACTION_INVALID")
            await self.page.mouse.move(*points[0])
            await self.page.mouse.down()
            try:
                for point in points[1:]:
                    await self.page.mouse.move(*point)
                    await asyncio.sleep(0.02)
            finally:
                await self.page.mouse.up()
        self.frame = None
        return {"submitted": True}

    async def diagnostic_check(self, mode="pool"):
        """One round per route, resumable at budget boundaries, with no unlock."""
        if not self.proxy or mode not in {"pool", "direct_diagnostic"}:
            raise failure("XHS_RPC_INPUT_INVALID")
        diagnostics = self.policy.data.setdefault("diagnostics", {})
        record = diagnostics.get(mode)
        if record and record["state"] in {"complete", "failed"}:
            return self.status()
        if record is None:
            self.policy.pause("XHS_ACCEPTANCE_PAUSED")
            record = diagnostics[mode] = {
                "state": "checking",
                "step": "page",
                "started_at": self.policy.clock(),
                "generation": self.proxy.data["generation"],
            }
            self.policy.save()
        self.proxy.data["paused"] = True
        self.proxy.save()
        self.diagnostic = True
        try:
            if self.proxy.route_mode != mode:
                await self.page.goto("about:blank")
                self.proxy.set_route(mode)
                self.owner_id, self.owner_at = None, 0
                self.note_cache.clear()
            await self.ensure_network()
            step = record["step"]
            if step == "page":
                self.policy.reserve("page", interactive=True)
                self.network_active = True
                response = await self.page.goto(
                    "https://www.xiaohongshu.com/explore",
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                evidence = await self.evidence()
                platform = parse_qs(urlsplit(self.page.url).query).get(
                    "error_code", [None]
                )[0]
                if (
                    platform
                    or evidence in {"platform_risk", "verification"}
                    or (response and response.status >= 400)
                ):
                    raise classify(
                        status=response.status if response else None,
                        platform=platform,
                        evidence=evidence,
                    )
                self.policy.success("page")
                record["step"] = "owner"
            elif step == "owner":
                await self.read("owner", {}, interactive=True)
                record["step"] = "note"
            else:
                # After restart, revalidate identity before consuming a note.
                if not self.owner_id:
                    await self.read("owner", {}, interactive=True)
                    return self.status()
                candidate = (
                    await asyncio.to_thread(self.mirror.candidate)
                    if self.mirror
                    else None
                )
                if not candidate:
                    raise failure("XHS_SOURCE_URL_REQUIRED")
                note = await self.read("note", {"url": candidate}, interactive=True)
                record.update(
                    state="complete",
                    note_id=note["note_id"],
                    checked_at=self.policy.clock(),
                )
                if mode == "pool":
                    self.proxy.data["node_validated_generation"] = self.proxy.data[
                        "generation"
                    ]
                    self.proxy.save()
                    self.policy.data["recovery"].update(
                        phase="validated",
                        last_verified_at=self.policy.clock(),
                        generation=self.proxy.data["generation"],
                    )
        except ProviderUnavailable as exc:
            if exc.code not in {"XHS_DEFERRED", "XHS_SESSION_BUSY"}:
                record.update(
                    state="failed",
                    error_code=exc.code,
                    platform_code=exc.diagnostics.get("platform_code"),
                    checked_at=self.policy.clock(),
                )
                self.network_failure(record["step"], exc)
            raise
        except Exception:  # noqa: BLE001 - redact signed browser URLs at the RPC boundary
            record.update(
                state="failed",
                error_code="XHS_NETWORK_ERROR",
                checked_at=self.policy.clock(),
            )
            raise failure("XHS_NETWORK_ERROR") from None
        finally:
            self.network_active = False
            self.diagnostic = False
            if record["state"] in {"complete", "failed"}:
                try:
                    await self.page.screenshot(
                        path=str(self.root / ("diagnostic-" + mode + ".jpg")),
                        type="jpeg",
                        quality=75,
                    )
                    record["screenshot_at"] = self.policy.clock()
                finally:
                    await self.page.goto("about:blank")
                    self.proxy.set_route("pool")
                    self.owner_id, self.owner_at = None, 0
            self.policy.save()
            await self.export()
            await self.publish()
        return self.status()

    async def recover(self):
        # Validation and backlog release are intentionally different operations.
        record = self.policy.data.get("diagnostics", {}).get("pool")
        if record and record["state"] in {"complete", "failed"}:
            remaining = record.get("checked_at", 0) + 1800 - self.policy.clock()
            if remaining > 0:
                raise failure("XHS_DEFERRED", delay=remaining + 1)
            self.archive_diagnostic("pool")
        return await self.diagnostic_check("pool")

    def archive_diagnostic(self, route):
        record = self.policy.data.setdefault("diagnostics", {}).pop(route, None)
        if record:
            history = self.policy.data.setdefault("diagnostic_history", [])
            history.append({"route": route, **record})
            self.policy.data["diagnostic_history"] = history[-20:]
            self.policy.save()

    async def start_acceptance(self):
        from .xhs_acceptance import candidates

        recovery = self.policy.data["recovery"]
        if (
            recovery["phase"] != "validated"
            or not self.proxy
            or self.proxy.route_mode != "pool"
            or recovery.get("generation") != self.proxy.data["generation"]
        ):
            raise failure("XHS_ACCEPTANCE_PAUSED", delay=60)
        selected = await asyncio.to_thread(candidates, self.mirror.config, self.video)
        if len(selected) != 3:
            raise failure("XHS_ACCEPTANCE_SAMPLES_REQUIRED")
        now = self.policy.clock()
        for value in self.policy.data["paths"].values():
            if value.get("code") in RISK_CODES:
                value["cleared_at"] = now
        recovery.update(
            phase="acceptance",
            reason="XHS_ACCEPTANCE_PAUSED",
            started_at=now,
            allowed_jobs=[r["job_id"] for r in selected],
            allowed_notes=[r["note_id"] for r in selected],
            samples=selected,
            accepted={},
            downloads={},
        )
        self.proxy.data.update(
            paused=False, cooldown_until=0, network_failures=0, backup_used=False
        )
        self.proxy.save()
        self.policy.save()
        await self.publish()
        return self.status()

    async def acceptance_tick(self):
        from .xhs_acceptance import advance, terminal_failures, verify_publication

        recovery = self.policy.data["recovery"]
        if recovery["phase"] not in {"acceptance", "observation"} or not self.mirror:
            return
        if recovery["phase"] == "acceptance":
            failures = await asyncio.to_thread(
                terminal_failures,
                self.mirror.config,
                self.video,
                recovery["allowed_jobs"],
            )
            if failures:
                self.policy.pause("XHS_ACCEPTANCE_FAILED")
                self.policy.data["recovery"]["failures"] = failures
                self.proxy.data.update(paused=True, reason="XHS_ACCEPTANCE_FAILED")
                self.proxy.save()
                self.policy.save()
                await self.publish()
                return
        verified = {}
        for job_id, receipt in list(recovery.get("downloads", {}).items()):
            if job_id in recovery.get("accepted", {}):
                continue
            result = await asyncio.to_thread(
                verify_publication, self.mirror.config, self.video, job_id, receipt
            )
            if result:
                verified[job_id] = result
        changed = advance(self.policy, verified)
        if changed and recovery["phase"] == "observation":
            await asyncio.to_thread(self.mirror.recovered, self.policy)
        await self.publish()

    async def login_tick(self):
        if not self.login or self.login["state"] not in {"waiting", "verifying"}:
            return
        if self.login["expires_at"] <= time.time():
            self.login["state"] = "expired"
            self.frame = None
            return
        if await self.stop_risky_login():
            return
        navigation = self.page.locator(
            "li.user.side-bar-component a[href*='/user/profile/']"
        ).first
        if (
            not await navigation.count()
            or not await navigation.is_visible()
            or await self.evidence() == "verification"
        ):
            return
        try:
            if self.login["state"] == "waiting":
                self.login["state"] = "verifying"
                self.frame = None
                return
            route = self.login.get("route", "pool")
            await self.diagnostic_check(route)
            record = self.policy.data.get("diagnostics", {}).get(route, {})
            if record.get("state") == "complete":
                self.login["state"] = "complete"
                self.policy.count("login_completed")
                self.policy.save()
            elif record.get("state") == "failed":
                self.login.update(state="blocked", error_code=record["error_code"])
        except ProviderUnavailable as exc:
            if exc.code not in {"XHS_DEFERRED", "XHS_SESSION_BUSY"}:
                self.login.update(state="blocked", error_code=exc.code)

    async def dispatch(self, operation, params):
        if operation == "status":
            return self.status()
        allowed = READS | {
            "login_start",
            "login_cancel",
            "frame",
            "pointer",
            "otp",
            "recover",
            "download_media",
            "network_pause",
            "network_resume",
            "diagnose",
            "acceptance_start",
            "acceptance_check",
        }
        if operation not in allowed:
            raise failure("XHS_RPC_INPUT_INVALID")
        if not isinstance(params, dict) or len(params.get("cursor", "")) > 4000:
            raise failure("XHS_RPC_INPUT_INVALID")
        if operation == "author" and not NOTE_ID.fullmatch(
            str(params.get("author_id", ""))
        ):
            raise failure("XHS_RPC_INPUT_INVALID")
        if self.lock.locked():
            raise failure("XHS_SESSION_BUSY", delay=5)
        async with self.lock:
            if operation in {"network_pause", "network_resume"}:
                if operation == "network_pause":
                    self.policy.pause("XHS_PROXY_PAUSED")
                elif self.policy.data["recovery"]["phase"] not in {
                    "observation",
                    "steady",
                }:
                    raise failure("XHS_ACCEPTANCE_PAUSED", delay=60)
                if self.proxy:
                    self.proxy.data["paused"] = operation == "network_pause"
                    self.proxy.save()
                await self.publish()
                return self.status()
            if operation == "diagnose":
                return await self.diagnostic_check(params.get("route", "pool"))
            if operation == "acceptance_start":
                return await self.start_acceptance()
            if operation == "acceptance_check":
                await self.acceptance_tick()
                return self.status()
            if operation == "download_media":
                return await self.download_media(params)
            if operation in READS:
                if self.active_login():
                    raise failure("XHS_DEFERRED", delay=30)
                if operation == "note":
                    self.policy.check(operation)
                    cached = self.note_cache.pop(params.get("url"), None)
                    if cached and time.time() - cached[0] < 120:
                        return cached[1]
                return await self.read(operation, params)
            if operation == "login_start":
                if self.proxy:
                    self.proxy.set_route(params.get("route", "pool"))
                self.diagnostic = True
                try:
                    await self.ensure_network()
                finally:
                    self.diagnostic = False
                return await self.begin_login()
            if operation == "login_cancel":
                self.check_attempt(params)
                self.login["state"], self.frame = "cancelled", None
                await self.page.goto("about:blank")
                if self.proxy:
                    self.proxy.set_route("pool")
                return self.status()
            if operation == "frame":
                return await self.login_frame(params)
            if operation in {"pointer", "otp"}:
                return await self.interact(operation, params)
            return await self.recover()


async def serve(video, config):
    from playwright.async_api import async_playwright

    service = SessionService(video, mirror=DatabaseMirror(config, video))
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    path = service.root / "session.sock"
    with browser_session_lock(service.root, timeout=5):
        async with async_playwright() as playwright:
            await service.start(playwright)

            async def handle(reader, writer):
                try:
                    credentials = writer.get_extra_info("socket").getsockopt(
                        socket.SOL_SOCKET, socket.SO_PEERCRED, 12
                    )
                    if struct.unpack("3i", credentials)[1] != os.getuid():
                        raise ValueError()
                    message = json.loads(
                        await asyncio.wait_for(reader.readline(), timeout=5)
                    )
                    result = await asyncio.wait_for(
                        service.dispatch(
                            message["operation"], message.get("params", {})
                        ),
                        timeout=None
                        if message["operation"] == "download_media"
                        else 180,
                    )
                    response = {"result": result}
                except ProviderUnavailable as exc:
                    response = {
                        "error": {
                            "code": exc.code,
                            "diagnostics": exc.diagnostics,
                            "status_code": exc.status_code,
                            "retryable": exc.retryable,
                        }
                    }
                except Exception:  # noqa: BLE001 - private RPC redaction boundary
                    response = {
                        "error": {
                            "code": "XHS_SESSION_UNAVAILABLE",
                            "diagnostics": {"retry_after": 30},
                        }
                    }
                try:
                    writer.write(json.dumps(response, ensure_ascii=False).encode())
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            path.unlink(missing_ok=True)
            server = await asyncio.start_unix_server(handle, str(path), limit=65536)
            path.chmod(0o600)
            print(
                json.dumps({"xhs_session_service": "ready", "version": 1}), flush=True
            )

            async def login_loop():
                observed_at = 0
                acceptance_at = 0
                while not stopped.is_set():
                    if time.time() - observed_at >= 3600:
                        service.observe()
                        observed_at = time.time()
                    if not service.lock.locked():
                        async with service.lock:
                            try:
                                await service.login_tick()
                                if not service.active_login():
                                    pending = next(
                                        (
                                            mode
                                            for mode, item in service.policy.data.get(
                                                "diagnostics", {}
                                            ).items()
                                            if item["state"] == "checking"
                                        ),
                                        None,
                                    )
                                    if pending:
                                        try:
                                            await service.diagnostic_check(pending)
                                        except ProviderUnavailable:
                                            pass  # Deferred does not consume a request; faults end the round.
                                if time.time() - acceptance_at >= 60:
                                    await service.acceptance_tick()
                                    acceptance_at = time.time()
                            except Exception:  # noqa: BLE001 - private browser errors are not logged
                                if service.login:
                                    service.login.update(
                                        state="blocked",
                                        error_code="XHS_SESSION_UNKNOWN",
                                    )
                    await asyncio.sleep(2)

            task = asyncio.create_task(login_loop())
            try:
                async with server:
                    await stopped.wait()
            finally:
                task.cancel()
                async with service.lock:
                    await service.export()
                    await service.context.close()
                    if service.proxy:
                        service.proxy.stop()
                path.unlink(missing_ok=True)


def main():
    from .config import load_config
    from .video_config import load_video_config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--video-config", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    asyncio.run(serve(load_video_config(args.video_config), load_config(args.config)))


if __name__ == "__main__":
    main()
