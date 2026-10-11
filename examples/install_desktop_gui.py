"""Install the native RAG queue monitor into the user's desktop and app menu."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

MARKER = "# Managed by rag_favorite/examples/install_desktop_gui.py"


def desktop_argument(value):
    value = str(value)
    if any(char in value for char in ("\n", "\r", "\0")):
        raise ValueError("Invalid desktop launcher argument")
    # Desktop Entry Exec quoting is distinct from shell quoting.
    escaped = value.replace("\\", "\\\\\\\\").replace('"', '\\"')
    escaped = escaped.replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
    return '"' + escaped + '"'


def launcher_text(root, python, config, video):
    arguments = [
        "/usr/bin/env",
        f"PYTHONPATH={root / 'src'}",
        python,
        "-m",
        "rag_favorite.desktop_gui",
        "--config",
        config,
        "--video-config",
        video,
    ]
    return "\n".join(
        [
            MARKER,
            "[Desktop Entry]",
            "Type=Application",
            "Version=1.0",
            "Name=RAG 任务控制台",
            "Name[zh_CN]=RAG 任务控制台",
            "Comment=查看真实任务队列与模型工作状态，启动或暂停处理",
            "Exec=" + " ".join(desktop_argument(arg) for arg in arguments),
            f"Path={root}",
            f"Icon={root / 'src/rag_favorite/resources/queue-monitor.svg'}",
            "Terminal=false",
            "Categories=Utility;",
            "StartupNotify=true",
            "StartupWMClass=rag-favorite-gui",
            "",
        ]
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument(
        "--config", type=Path, default=root / ".runtime/phase1/config.toml"
    )
    parser.add_argument(
        "--video-config", type=Path, default=root / ".runtime/videorag/video.toml"
    )
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)
    home = Path.home()
    desktop = home / "Desktop"
    if shutil.which("xdg-user-dir"):
        result = subprocess.run(
            ["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, check=False
        )
        if result.returncode == 0 and result.stdout.strip():
            desktop = Path(result.stdout.strip())
    destinations = (
        home / ".local/share/applications/rag-favorite-gui.desktop",
        desktop / "RAG任务控制台.desktop",
    )
    for path in destinations:
        if path.exists() and MARKER not in path.read_text():
            raise RuntimeError(f"已有其他启动器，请保留或重命名：{path}")
    if args.uninstall:
        for path in destinations:
            path.unlink(missing_ok=True)
        print("已移除桌面与应用菜单启动器。")
        return 0
    if not args.config.is_file() or not args.video_config.is_file():
        raise RuntimeError("需要有效的主配置和视频配置路径。")
    text = launcher_text(
        root, Path(sys.executable), args.config.resolve(), args.video_config.resolve()
    )
    for path in destinations:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
    if shutil.which("gio"):
        subprocess.run(
            ["gio", "set", str(destinations[1]), "metadata::trusted", "true"],
            capture_output=True,
            check=False,
        )
    if shutil.which("update-desktop-database"):
        subprocess.run(
            ["update-desktop-database", str(destinations[0].parent)],
            capture_output=True,
            check=False,
        )
    for path in destinations:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
