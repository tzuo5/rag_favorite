"""Authenticated phone interaction with the dedicated XHS login surface."""

import asyncio
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .video_provider import ProviderUnavailable
from .xhs_session_rpc import SessionRPC


class XhsAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal[
        "login_start",
        "login_cancel",
        "recover",
        "pointer",
        "otp",
        "network_pause",
        "network_resume",
        "acceptance_start",
    ]
    request_id: UUID
    route: Literal["pool", "direct_diagnostic"] | None = None
    attempt_id: UUID | None = None
    frame_id: UUID | None = None
    code: str | None = Field(default=None, pattern=r"^[0-9]{4,8}$")
    points: list[tuple[float, float]] | None = Field(
        default=None, min_length=1, max_length=80
    )


def install_routes(app, monitor, session, csrf):
    action_lock = asyncio.Lock()

    async def call(operation, **params):
        video = getattr(monitor, "video", None)
        if video is None:
            raise HTTPException(503, "小红书会话服务尚未配置。")
        try:
            return await asyncio.to_thread(SessionRPC(video).call, operation, **params)
        except ProviderUnavailable as exc:
            raise HTTPException(409, exc.code or "XHS_SESSION_UNAVAILABLE") from None

    @app.get("/api/xhs/session")
    async def state(request: Request):
        session(request)
        return await call("status")

    @app.get("/api/xhs/frame")
    async def frame(request: Request, attempt_id: UUID):
        session(request)
        return await call("frame", attempt_id=str(attempt_id))

    @app.post("/api/xhs/action")
    async def action(data: XhsAction, request: Request):
        user = session(request)
        csrf(request, user)
        if data.request_id in user["requests"]:
            raise HTTPException(409, "重复操作已拒绝，请刷新查看结果。")
        if action_lock.locked():
            raise HTTPException(409, "小红书操作正在执行，请稍后查看。")
        params = data.model_dump(mode="json", exclude_none=True)
        operation = params.pop("action")
        params.pop("request_id")
        if operation in {"pointer", "otp"} and not (data.attempt_id and data.frame_id):
            raise HTTPException(422, "请先刷新验证页面。")
        async with action_lock:
            user["requests"].append(data.request_id)
            return await call(operation, **params)
