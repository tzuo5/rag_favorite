"""Owner-only HTTPS queue console, behind Tailscale Serve on loopback.

Run one worker with proxy_headers=False: only Serve's overwritten headers are
trusted. Local users with the service owner's privileges are outside this
boundary, as they can already read the database and credential files.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import stat
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

COOKIE = "__Host-rag-session"
SESSION_SECONDS = 8 * 3600
ASSETS = Path(__file__).parent / "resources" / "web"


def password_hash(password: str, salt: str) -> str:
    return hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1
    ).hex()


def write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)


def initialize(path: Path) -> None:
    """Bootstrap once from the signed-in owner's existing devices; no secrets on stdout."""
    import subprocess

    if path.exists():
        raise ValueError("配置已存在；初始化不会覆盖密码或设备白名单。")
    state = json.loads(subprocess.check_output(["tailscale", "status", "--json"]))
    own = state["Self"]
    owner = state["User"][str(own["UserID"])]["LoginName"]
    devices = [own] + [
        peer
        for peer in state.get("Peer", {}).values()
        if peer.get("UserID") == own["UserID"] and not peer.get("Tags")
    ]
    password = secrets.token_urlsafe(24)
    salt = secrets.token_hex(16)
    config = {
        "origin": "https://" + own["DNSName"].rstrip("."),
        "owner_login": owner,
        "allowed_ips": [ip for device in devices for ip in device["TailscaleIPs"]],
        "self_ips": own["TailscaleIPs"],
        "username": "admin",
        "salt": salt,
        "password_hash": password_hash(password, salt),
    }
    # Write the bootstrap secret first, so a failure cannot lose the only password.
    write_private(
        path.parent / "initial-password.txt", "用户名：admin\n密码：" + password + "\n"
    )
    write_private(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    print("配置与初始密码已保存到仅当前用户可读的目录：" + str(path.parent))


def load_settings(path: Path) -> dict:
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("网页配置必须归当前用户所有，权限必须为 600。")
    settings = json.loads(path.read_text())
    origin = urlsplit(settings["origin"])
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.path
        or origin.query
        or origin.fragment
    ):
        raise ValueError("origin 必须是完整 HTTPS 网站地址，不含路径。")
    settings["allowed_ips"] = {
        str(ipaddress.ip_address(x)) for x in settings["allowed_ips"]
    }
    settings["self_ips"] = {str(ipaddress.ip_address(x)) for x in settings["self_ips"]}
    if (
        not settings["allowed_ips"]
        or not settings["self_ips"] <= settings["allowed_ips"]
    ):
        raise ValueError("设备白名单不完整。")
    if not settings["owner_login"] or not settings["username"]:
        raise ValueError("缺少所有者或登录用户名。")
    if (
        len(bytes.fromhex(settings["salt"])) != 16
        or len(bytes.fromhex(settings["password_hash"])) != 64
    ):
        raise ValueError("密码配置无效。")
    return settings


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(max_length=100)
    password: str = Field(max_length=256)


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["start", "stop", "start_job", "stop_job"]
    job_id: UUID | None = None
    request_id: UUID


JOB_FIELDS = (
    "id",
    "title",
    "collection",
    "state",
    "stage",
    "stage_label",
    "model",
    "progress",
    "state_label",
    "paused",
    "queue_rank",
    "error_code",
    "attempts",
    "priority",
    "created_at",
    "updated_at",
    "media_expired",
    "pipeline_stage",
    "pipeline_status",
    "retry_at",
    "blocked_reason",
    "stage_owner",
)
MODEL_FIELDS = (
    "key",
    "role",
    "name",
    "state",
    "task",
    "connection",
    "activity",
)
CALL_FIELDS = (
    "call_id",
    "model",
    "job_id",
    "pid",
    "status",
    "operation",
    "started_at",
    "updated_at",
    "completed_at",
    "duration_seconds",
    "http_status",
    "stream_complete",
    "valid_output",
    "request_bytes",
    "image_bytes",
    "images",
    "error_category",
    "retryable",
    "request_id",
)
METRIC_FIELDS = (
    "window_start",
    "window_end",
    "scope",
    "retained_calls",
    "attempts",
    "http_success",
    "valid_outputs",
    "unknown_attempts",
    "searchable_completed",
    "publication_scope",
)
PHASE_FIELDS = (
    "job_id",
    "stage",
    "status",
    "owner",
    "retry_at",
    "error_code",
    "elapsed_seconds",
)


def public_snapshot(value: dict) -> dict:
    """An explicit allowlist keeps provider configs and future payload fields private."""
    result = {
        key: value[key]
        for key in (
            "captured_at",
            "counts",
            "total",
            "paused_pending",
            "control_supported",
            "truncated",
        )
    }
    result["jobs"] = [
        {key: row.get(key) for key in JOB_FIELDS} for row in value["jobs"]
    ]
    result["models"] = [
        {key: row.get(key) for key in MODEL_FIELDS} for row in value["models"]
    ]
    result["worker"] = {
        key: value["worker"].get(key) for key in ("ActiveState", "SubState", "MainPID")
    }
    result["control"] = {
        key: value["control"].get(key) for key in ("paused", "paused_jobs")
    }
    result["logs"] = value["logs"]  # QueueMonitor exposes structured fields only.
    pipeline = value.get("pipeline", {})
    result["pipeline"] = {
        "available": pipeline.get("available", False),
        "circuit_until": pipeline.get("circuit_until"),
        "stages": {
            stage: {
                key: row.get(key, 0)
                for key in ("waiting", "running", "retry_wait", "blocked")
            }
            for stage, row in pipeline.get("stages", {}).items()
            if stage in {"download", "prepare", "llm", "publish"}
        },
        "jobs": [
            {key: row.get(key) for key in PHASE_FIELDS}
            for row in pipeline.get("jobs", [])
        ],
        "running": [
            {key: row.get(key) for key in PHASE_FIELDS}
            for row in pipeline.get("running", [])
        ],
    }
    result["call_metrics"] = {
        key: value.get("call_metrics", {}).get(key) for key in METRIC_FIELDS
    }
    result["model_calls"] = [
        {key: row.get(key) for key in CALL_FIELDS}
        for row in value.get("model_calls", [])
    ]
    return result


def create_app(monitor, settings: dict) -> FastAPI:
    sessions = {}
    attempts = {}
    global_attempts = deque()
    snapshot = None
    last_good = 0.0
    read_error = False
    backend_lock = threading.Lock()
    action_lock = asyncio.Lock()

    def read_backend():
        with backend_lock:
            return public_snapshot(monitor.snapshot())

    async def refresh():
        nonlocal snapshot, last_good, read_error
        try:
            snapshot = await asyncio.to_thread(read_backend)
            last_good, read_error = time.monotonic(), False
        except Exception:  # noqa: BLE001 - never expose provider credentials
            # Database/provider exception strings may include credentials or URLs.
            read_error = True

    async def poll():
        while True:
            await refresh()
            await asyncio.sleep(3)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(poll())
        app.state.refresh = refresh
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default response echoes invalid input, including passwords.
        return JSONResponse({"detail": "请求参数无效。"}, status_code=422)

    def prune():
        now = time.monotonic()
        for key in list(sessions):
            if sessions[key]["expires"] <= now:
                del sessions[key]
        for key in list(attempts):
            if not attempts[key] or attempts[key][-1] < now - 600:
                del attempts[key]
        while global_attempts and global_attempts[0] <= now - 60:
            global_attempts.popleft()

    def session(request):
        prune()
        token = request.cookies.get(COOKIE, "")
        value = sessions.get(token)
        if not value or value["ip"] != request.state.device_ip:
            raise HTTPException(401, "请登录。")
        return value

    def csrf(request, value):
        if not hmac.compare_digest(
            request.headers.get("x-csrf-token", "").encode(), value["csrf"].encode()
        ):
            raise HTTPException(403, "操作验证失败，请刷新网页。")

    @app.middleware("http")
    async def private_boundary(request: Request, call_next):
        try:
            client = request.client.host if request.client else ""
            source = str(
                ipaddress.ip_address(request.headers.get("x-forwarded-for", ""))
            )
            login = request.headers.get("tailscale-user-login", "")
            local_self = source in settings["self_ips"] and not login
            allowed = (
                client in {"127.0.0.1", "::1"}
                and request.headers.get("host") == urlsplit(settings["origin"]).netloc
                and request.headers.get("x-forwarded-proto") == "https"
                and not request.headers.get("tailscale-funnel-request")
                and source in settings["allowed_ips"]
                and (login == settings["owner_login"] or local_self)
            )
        except ValueError:
            allowed = False
        if not allowed:
            response = JSONResponse(
                {"detail": "此设备或访问入口未获授权。"}, status_code=403
            )
        elif (
            request.method not in {"GET", "HEAD"}
            and request.headers.get("origin") != settings["origin"]
        ):
            response = JSONResponse({"detail": "请求来源未获授权。"}, status_code=403)
        else:
            request.state.device_ip = source
            # Bound bodies before parsing credentials or actions; do not trust Content-Length.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 4096:
                    return JSONResponse({"detail": "请求过大。"}, status_code=413)
            request._body = bytes(body)
            response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Strict-Transport-Security": "max-age=31536000",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        return response

    @app.get("/")
    async def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/assets/{filename}")
    async def asset(filename: Literal["console.js", "console.css"]):
        return FileResponse(ASSETS / filename)

    @app.post("/api/login")
    async def login(data: Login, request: Request):
        prune()
        now, ip = time.monotonic(), request.state.device_ip
        failures = attempts.setdefault(ip, deque(maxlen=5))
        if (len(failures) == 5 and failures[0] > now - 600) or len(
            global_attempts
        ) >= 30:
            raise HTTPException(
                429, "登录尝试过多，请稍后再试。", headers={"Retry-After": "600"}
            )
        global_attempts.append(now)
        failures.append(now)
        verified = await asyncio.to_thread(
            password_hash, data.password, settings["salt"]
        )
        if not (
            hmac.compare_digest(data.username.encode(), settings["username"].encode())
            and hmac.compare_digest(verified, settings["password_hash"])
        ):
            raise HTTPException(401, "用户名或密码错误。")
        if len(sessions) >= 128:
            raise HTTPException(429, "会话过多，请退出其他会话或稍后重试。")
        attempts.pop(ip, None)
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        token = secrets.token_urlsafe(32)
        value = {
            "csrf": secrets.token_urlsafe(32),
            "ip": ip,
            "expires": now + SESSION_SECONDS,
            "requests": deque(maxlen=256),
        }
        sessions[token] = value
        response = JSONResponse({"csrf": value["csrf"]})
        response.set_cookie(
            COOKIE,
            token,
            max_age=SESSION_SECONDS,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/api/session")
    async def current_session(request: Request):
        return {"csrf": session(request)["csrf"]}

    @app.post("/api/logout")
    async def logout(request: Request):
        csrf(request, session(request))
        sessions.pop(request.cookies.get(COOKIE, ""), None)
        response = JSONResponse({"message": "已退出。"})
        response.delete_cookie(
            COOKIE, secure=True, httponly=True, samesite="strict", path="/"
        )
        return response

    @app.get("/api/snapshot")
    async def get_snapshot(request: Request):
        session(request)
        age = time.monotonic() - last_good if last_good else None
        stale = read_error or age is None or age > 15
        return JSONResponse(
            {
                "snapshot": snapshot,
                "stale": stale,
                "age_seconds": round(age, 1) if age is not None else None,
                "error": "数据暂不可用或已过时，正在自动重连。" if stale else None,
            },
            status_code=503 if stale else 200,
        )

    @app.post("/api/action")
    async def action(data: Action, request: Request):
        value = session(request)
        csrf(request, value)
        if data.action.endswith("_job") != (data.job_id is not None):
            raise HTTPException(400, "任务操作必须指定任务，全局操作不得指定任务。")
        if data.request_id in value["requests"]:
            raise HTTPException(409, "重复操作已拒绝，请刷新查看结果。")
        if action_lock.locked():
            raise HTTPException(409, "另一项操作正在执行，请等待结果。")
        async with action_lock:
            value["requests"].append(data.request_id)

            def execute():
                with backend_lock:
                    return monitor.action(
                        data.action, str(data.job_id) if data.job_id else None
                    )

            try:
                message = await asyncio.to_thread(execute)
            except Exception:  # noqa: BLE001 - no exception strings in HTTP responses
                raise HTTPException(
                    409, "操作未能确认完成，请刷新状态；检查任务状态、worker 或控制锁。"
                ) from None
            await refresh()
            return {"message": message}

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="RAG 私有网页控制台")
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--reset-password", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--video-config", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.init:
        initialize(args.settings)
        return
    settings = load_settings(args.settings)
    if args.reset_password:
        import getpass

        password = getpass.getpass("新密码（至少 16 个字符）：")
        if len(password) < 16 or password != getpass.getpass("再次输入："):
            raise ValueError("密码长度不足或两次输入不一致。")
        raw = json.loads(args.settings.read_text())
        raw["salt"] = secrets.token_hex(16)
        raw["password_hash"] = password_hash(password, raw["salt"])
        temporary = args.settings.with_suffix(".new")
        write_private(temporary, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(args.settings)
        (args.settings.parent / "initial-password.txt").unlink(missing_ok=True)
        print("密码已更新；请重启网页服务以清除旧会话并加载新密码。")
        return
    import uvicorn

    from .desktop_gui import profiles
    from .queue_monitor import QueueMonitor

    config, video = profiles(args.config, args.video_config)
    app = create_app(QueueMonitor(config, video), settings)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        proxy_headers=False,
        access_log=False,
        workers=1,
    )


if __name__ == "__main__":
    main()
