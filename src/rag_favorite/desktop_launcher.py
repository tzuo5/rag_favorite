"""Open the existing private web console in the user's default browser."""

import argparse
import json
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


def console_url(settings):
    origin = json.loads(Path(settings).read_text())["origin"]
    parts = urlsplit(origin)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
        or any(c.isspace() or ord(c) < 32 for c in origin)
    ):
        raise ValueError("网页配置需要不含凭据的 HTTPS 网站地址。")
    return origin.rstrip("/") + "/"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path(__file__).resolve().parents[2] / ".runtime/web/settings.json",
    )
    parser.add_argument("--print-url", action="store_true")
    args = parser.parse_args(argv)
    url = console_url(args.settings)
    if args.print_url:
        print(url)
        return 0
    return subprocess.run(["xdg-open", url], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
