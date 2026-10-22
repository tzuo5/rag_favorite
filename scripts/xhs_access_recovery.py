"""Bounded XHS diagnostics and local acceptance through the existing session owner."""

import argparse
import json
import time

from rag_favorite.video_config import load_video_config
from rag_favorite.video_provider import ProviderUnavailable
from rag_favorite.xhs_session_rpc import SessionRPC


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=("status", "diagnose", "acceptance_start", "acceptance_check"),
    )
    parser.add_argument("--video-config", default=".runtime/videorag/video.toml")
    parser.add_argument(
        "--route", choices=("direct_diagnostic", "pool"), default="pool"
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Poll local diagnostic state for at most 10 minutes; never starts another round.",
    )
    args = parser.parse_args()
    rpc = SessionRPC(load_video_config(args.video_config))
    error = None
    try:
        params = {"route": args.route} if args.operation == "diagnose" else {}
        rpc.call(args.operation, **params)
    except ProviderUnavailable as exc:
        error = {"code": exc.code, "diagnostics": exc.diagnostics}
    deadline = time.monotonic() + 600
    while True:
        status = rpc.call("status")
        diagnostic = status.get("diagnostics", {}).get(args.route, {})
        if (
            not args.wait
            or args.operation != "diagnose"
            or diagnostic.get("state") != "checking"
            or time.monotonic() >= deadline
        ):
            break
        time.sleep(2)
    print(json.dumps({"error": error, "status": status}, ensure_ascii=False, indent=2))
    if args.operation == "diagnose":
        if diagnostic.get("state") == "failed":
            return 1
        return 0 if diagnostic.get("state") == "complete" else 2
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
