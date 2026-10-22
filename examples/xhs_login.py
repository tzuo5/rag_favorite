"""Compatibility CLI for the private phone login page; never opens a profile."""

import argparse
import json
import time
from pathlib import Path

from rag_favorite.video_config import load_video_config
from rag_favorite.xhs_session_rpc import SessionRPC


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--video-config", type=Path, default=Path(".runtime/videorag/video.toml")
    )
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Compatibility flag; browser is owned by the service",
    )
    args = parser.parse_args()
    rpc = SessionRPC(load_video_config(args.video_config))
    state = rpc.call("login_start", wait=True)
    print("请打开私有 RAG 控制台的小红书登录面板，手机扫码或完成验证。", flush=True)
    deadline = time.monotonic() + min(args.timeout, 600)
    while time.monotonic() < deadline:
        state = rpc.call("status")
        login = state.get("login") or {}
        if login.get("state") not in {"waiting", "verifying"}:
            print(json.dumps({"login": login}, ensure_ascii=False), flush=True)
            return
        time.sleep(3)
    print("CLI 等待结束；原浏览器会话仍由服务保留。", flush=True)


if __name__ == "__main__":
    main()
