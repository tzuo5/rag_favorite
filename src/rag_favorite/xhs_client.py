"""Owner-authenticated reads through the single persistent session service."""

from __future__ import annotations

import re

from .xhs_session_rpc import SessionRPC

NOTE_ID = re.compile(r"^[a-f0-9]{24}$")


class XhsSessionClient:
    def __init__(self, video):
        self.video = video
        self.rpc = SessionRPC(video)

    def owner(self):
        return self.rpc.call("owner", wait=True)

    def page(self, owner, cursor):
        return self.rpc.call("favorites", cursor=cursor, wait=True)

    def get(self, uri, params=None):
        params = params or {}
        if uri == "/api/sns/web/v2/user/me":
            return {"user_id": self.owner(), "guest": False}
        if uri == "/api/sns/web/v2/note/collect/page":
            return self.page(params.get("user_id"), params.get("cursor", ""))
        if uri == "/api/sns/web/v1/user_posted":
            return self.rpc.call(
                "author",
                author_id=params["user_id"],
                cursor=params.get("cursor", ""),
                wait=True,
            )
        raise ValueError("Unsupported XHS read operation")
