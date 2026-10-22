"""Discover the owner's following list from the authenticated page's reads."""

from __future__ import annotations

from .video_provider import ProviderUnavailable
from .xhs_client import NOTE_ID, XhsSessionClient

FOLLOWING_ALL = "/api/im/web/users/following/all"


def normalize_following(data, expected_count):
    """The IM 'all' response is complete only when the exact profile count agrees."""
    users = data.get("follow_user_d_t_o_list") if isinstance(data, dict) else None
    if (
        not isinstance(users, list)
        or type(expected_count) is not int
        or expected_count < 0
    ):
        raise ProviderUnavailable("FOLLOWING_ENUMERATION_UNAVAILABLE")
    result = {}
    for user in users:
        if not isinstance(user, dict) or not NOTE_ID.fullmatch(
            str(user.get("user_id", ""))
        ):
            raise ProviderUnavailable("XHS_FOLLOWING_SCHEMA_CHANGED")
        uid = user["user_id"]
        result[uid] = {
            "author_id": uid,
            "nickname": str(user.get("nick_name", ""))[:300],
        }
    if data.get("has_more") not in {None, False} or len(result) != expected_count:
        raise ProviderUnavailable("XHS_FOLLOWING_COUNT_MISMATCH")
    return list(result.values())


class FollowingClient(XhsSessionClient):
    def following(self, owner):
        return self.rpc.call("following", wait=True)

    def author_page(self, author_id, cursor):
        return self.get(
            "/api/sns/web/v1/user_posted",
            {
                "user_id": author_id,
                "num": 30,
                "cursor": cursor,
                "image_formats": "jpg,webp,avif",
                "xsec_source": "pc_user",
            },
        )
