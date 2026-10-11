"""Install or remove the owner-only web service, without changing worker services."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "rag-favorite-web.service"
MARKER = "# Managed by rag_favorite/examples/install_web_console.py\n"


def quote(value):
    return json.dumps(str(value).replace("%", "%%"), ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    target = Path.home() / ".config/systemd/user" / NAME
    if target.exists() and not target.read_text().startswith(MARKER):
        raise SystemExit("同名服务不属于此安装器，未修改。")
    if args.uninstall:
        if target.exists():
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", NAME], check=True
            )
            target.unlink()
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        print("网页服务已卸载，凭据保留；私有 HTTPS 映射请按文档单独关闭。")
        return
    settings = ROOT / ".runtime/web/settings.json"
    if not settings.exists():
        raise SystemExit("请先运行 web_console --init 初始化私有配置。")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        MARKER
        + f"""[Unit]
Description=RAG private web queue console
After=network.target

[Service]
Type=simple
WorkingDirectory={str(ROOT).replace("%", "%%")}
Environment={quote("PYTHONPATH=" + str(ROOT / "src"))}
Environment=PYTHONUNBUFFERED=1
ExecStart={quote(ROOT / ".venv/bin/python")} -m rag_favorite.web_console --settings {quote(settings)} --config {quote(ROOT / ".runtime/phase1/config.toml")} --video-config {quote(ROOT / ".runtime/videorag/video.toml")}
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={quote(ROOT / ".runtime")}
ReadWritePaths=-{quote(Path.home() / ".local/share/rag-favorite")}
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=default.target
"""
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", NAME], check=True)
    print("私有网页服务已安装；HTTPS 与跨设备访问需单独验收。")


if __name__ == "__main__":
    main()
