"""Install the private web console shortcut into the desktop and app menu."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from rag_favorite.desktop_launcher import console_url

MARKER = "# Managed by rag_favorite/examples/install_desktop_gui.py"


def desktop_argument(value):
    value = str(value)
    if any(char in value for char in ("\n", "\r", "\0")):
        raise ValueError("Invalid desktop launcher argument")
    # Desktop Entry Exec quoting is distinct from shell quoting.
    escaped = value.replace("\\", "\\\\\\\\").replace('"', '\\"')
    escaped = escaped.replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
    return '"' + escaped + '"'


def launcher_text(root, url):
    arguments = ["xdg-open", url]
    return "\n".join(
        [
            MARKER,
            "[Desktop Entry]",
            "Type=Application",
            "Version=1.0",
            "Name=RAG 任务控制台",
            "Name[zh_CN]=RAG 任务控制台",
            "Comment=在浏览器中打开私有 RAG 任务控制台",
            "Exec=" + " ".join(desktop_argument(arg) for arg in arguments),
            f"Path={root}",
            f"Icon={root / 'src/rag_favorite/resources/queue-monitor.svg'}",
            "Terminal=false",
            "Categories=Utility;",
            "StartupNotify=false",
            "",
        ]
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument(
        "--settings", type=Path, default=root / ".runtime/web/settings.json"
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
    text = launcher_text(root, console_url(args.settings))
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
