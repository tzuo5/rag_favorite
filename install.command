#!/bin/bash
set -euo pipefail

VERSION="1.2.0"
EXPECTED_ARCH="arm64"
WHEEL_NAME="rag_favorite-1.2.0-py3-none-any.whl"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
PAYLOAD="$SCRIPT_DIR/payload"
PREFIX="$HOME/Library/Application Support/rag-favorite-cli"
BIN_DIR="$HOME/.local/bin"
ASSUME_YES=false
SKIP_BROWSER=false
CONFIGURE_SHELL=true
CURRENT_STEP="preflight"

step() {
  CURRENT_STEP="$1"
  printf '\n==> %s\n' "$CURRENT_STEP"
}

on_error() {
  status=$?
  echo >&2
  echo "Installation stopped during: $CURRENT_STEP" >&2
  echo "Nothing outside the managed prefix was removed." >&2
  echo "After fixing the reported problem, rerun the same install.command; setup is idempotent." >&2
  exit "$status"
}
trap on_error ERR

usage() {
  echo "Usage: ./install.command [--prefix PATH] [--bin-dir PATH] [--yes] [--skip-browser] [--no-shell-config]"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix)
      PREFIX="${2:?--prefix requires a path}"
      shift 2
      ;;
    --bin-dir)
      BIN_DIR="${2:?--bin-dir requires a path}"
      shift 2
      ;;
    --yes)
      ASSUME_YES=true
      shift
      ;;
    --skip-browser)
      SKIP_BROWSER=true
      shift
      ;;
    --no-shell-config)
      CONFIGURE_SHELL=false
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This installer can only run on macOS." >&2
  exit 1
fi
if [ "$(uname -m)" != "$EXPECTED_ARCH" ]; then
  echo "This package is for $EXPECTED_ARCH, but this Mac reports $(uname -m)." >&2
  exit 1
fi
case "$PREFIX" in
  ""|/|"$HOME")
    echo "Unsafe installation prefix: $PREFIX" >&2
    exit 1
    ;;
esac
case "$PREFIX:$BIN_DIR" in
  /*:/*) ;;
  *) echo "Installation and command paths must be absolute." >&2; exit 1 ;;
esac

cd "$SCRIPT_DIR"
step "Verify the signed release payload"
if ! CHECKSUM_OUTPUT="$(/usr/bin/shasum -a 256 -c SHA256SUMS 2>&1)"; then
  echo "$CHECKSUM_OUTPUT" >&2
  exit 1
fi
echo "Payload integrity: verified $(wc -l < SHA256SUMS | tr -d ' ') files."

for link in rag-favorite rag-favorite-mcp rag-favorite-web; do
  target="$BIN_DIR/$link"
  if [ -e "$target" ] && [ ! -L "$target" ]; then
    echo "Refusing to replace non-symlink executable: $target" >&2
    exit 1
  fi
  if [ -L "$target" ] && [ "$(readlink "$target")" != "$PREFIX/bin/$link" ]; then
    echo "Refusing to replace unmanaged command link: $target" >&2
    exit 1
  fi
done

if [ "$ASSUME_YES" != true ]; then
  echo "rag-favorite $VERSION will be installed in:"
  echo "  $PREFIX"
  echo "Commands will be linked in:"
  echo "  $BIN_DIR"
  read -r -p "Continue? [y/N] " answer
  case "$answer" in
    y|Y|yes|YES) ;;
    *) echo "Cancelled."; exit 0 ;;
  esac
fi

if [ -e "$PREFIX" ] && [ ! -f "$PREFIX/.rag-favorite-macos-install" ]; then
  echo "Refusing to write into an unmanaged installation prefix: $PREFIX" >&2
  exit 1
fi
INGESTION="$PREFIX/ingestion"
if [ -e "$INGESTION/.venv" ] && [ ! -L "$INGESTION/.venv" ]; then
  echo "Refusing to replace unmanaged ingestion virtual environment." >&2
  exit 1
fi
mkdir -p "$PREFIX" "$PREFIX/runtime" "$PREFIX/wheels" "$BIN_DIR"
chmod 700 "$PREFIX" "$PREFIX/runtime"
touch "$PREFIX/.rag-favorite-macos-install"
printf '%s\n' "$VERSION" > "$PREFIX/.rag-favorite-macos-install"

if [ -f "$INGESTION/.managed-files" ]; then
  while IFS= read -r relative; do
    case "$relative" in
      ""|/*|*".."*) continue ;;
    esac
    rm -f "$INGESTION/$relative"
  done < "$INGESTION/.managed-files"
fi
mkdir -p "$INGESTION"
step "Copy application files without touching private .env data"
/usr/bin/ditto "$PAYLOAD/ingestion" "$INGESTION"
cp "$PAYLOAD/$WHEEL_NAME" "$PREFIX/wheels/$WHEEL_NAME"

VENV="$PREFIX/runtime/venv"
UV="$PAYLOAD/uv"
export UV_PYTHON_INSTALL_DIR="$PREFIX/runtime/python"
export UV_CACHE_DIR="$PREFIX/runtime/uv-cache"
step "Provision isolated Python 3.12 (downloaded once, then cached)"
"$UV" python install 3.12
"$UV" venv --clear --python 3.12 "$VENV"
step "Install rag-favorite and reviewed Python dependencies"
"$UV" pip install --python "$VENV/bin/python" "$PREFIX/wheels/$WHEEL_NAME"
# rag-favorite exposes MCP only over local stdio. Installing the SDK's reviewed
# stdio dependency set avoids its unused HTTP OAuth crypto extra, for which
# upstream no longer publishes an Intel macOS wheel.
"$UV" pip install --python "$VENV/bin/python" \
  --requirement "$PAYLOAD/mcp-stdio-requirements.txt"
"$UV" pip install --python "$VENV/bin/python" --no-deps "mcp==1.29.0"
"$UV" pip install --python "$VENV/bin/python" --requirement "$INGESTION/requirements.txt"
"$UV" pip check --python "$VENV/bin/python"
if [ "$SKIP_BROWSER" != true ]; then
  step "Install optional Playwright Chromium"
  if ! "$VENV/bin/python" -m playwright install chromium; then
    echo "Warning: Chromium download failed; CLI, MCP, and the web UI are still installed." >&2
    echo "Retry later with: \"$VENV/bin/python\" -m playwright install chromium" >&2
  fi
fi

ln -sfn "$VENV" "$INGESTION/.venv"

mkdir -p "$PREFIX/bin"
cat > "$PREFIX/bin/rag-favorite" <<'LAUNCHER'
#!/bin/bash
set -euo pipefail
LAUNCHER_PATH="$0"
[ ! -L "$LAUNCHER_PATH" ] || LAUNCHER_PATH="$(readlink "$LAUNCHER_PATH")"
APP_ROOT="$(cd "$(dirname "$LAUNCHER_PATH")/.." && pwd -P)"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/Library/Preferences}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/Library/Application Support}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/Library/Caches}"
export XDG_STATE_HOME="${XDG_STATE_HOME:-$HOME/Library/Application Support}"
export RAG_FAVORITE_CONFIG="${RAG_FAVORITE_CONFIG:-$XDG_CONFIG_HOME/rag-favorite/config.toml}"
export RAG_FAVORITE_INGESTION_DIR="$APP_ROOT/ingestion"
exec "$APP_ROOT/runtime/venv/bin/python" -m rag_favorite.cli "$@"
LAUNCHER
cat > "$PREFIX/bin/rag-favorite-mcp" <<'LAUNCHER'
#!/bin/bash
set -euo pipefail
LAUNCHER_PATH="$0"
[ ! -L "$LAUNCHER_PATH" ] || LAUNCHER_PATH="$(readlink "$LAUNCHER_PATH")"
APP_ROOT="$(cd "$(dirname "$LAUNCHER_PATH")/.." && pwd -P)"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/Library/Preferences}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/Library/Application Support}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/Library/Caches}"
export XDG_STATE_HOME="${XDG_STATE_HOME:-$HOME/Library/Application Support}"
export RAG_FAVORITE_CONFIG="${RAG_FAVORITE_CONFIG:-$XDG_CONFIG_HOME/rag-favorite/config.toml}"
export RAG_FAVORITE_INGESTION_DIR="$APP_ROOT/ingestion"
exec "$APP_ROOT/runtime/venv/bin/python" -m rag_favorite.mcp_server "$@"
LAUNCHER
cat > "$PREFIX/bin/rag-favorite-web" <<'LAUNCHER'
#!/bin/bash
set -euo pipefail
LAUNCHER_PATH="$0"
[ ! -L "$LAUNCHER_PATH" ] || LAUNCHER_PATH="$(readlink "$LAUNCHER_PATH")"
APP_ROOT="$(cd "$(dirname "$LAUNCHER_PATH")/.." && pwd -P)"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/Library/Preferences}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/Library/Application Support}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/Library/Caches}"
export XDG_STATE_HOME="${XDG_STATE_HOME:-$HOME/Library/Application Support}"
export RAG_FAVORITE_CONFIG="${RAG_FAVORITE_CONFIG:-$XDG_CONFIG_HOME/rag-favorite/config.toml}"
export RAG_FAVORITE_INGESTION_DIR="$APP_ROOT/ingestion"
set -a
[ ! -f "$APP_ROOT/ingestion/.env" ] || . "$APP_ROOT/ingestion/.env"
set +a
cd "$APP_ROOT/ingestion"
exec "$APP_ROOT/runtime/venv/bin/python" start.py "$@"
LAUNCHER
chmod 700 "$PREFIX/bin/rag-favorite" "$PREFIX/bin/rag-favorite-mcp" "$PREFIX/bin/rag-favorite-web"
ln -sfn "$PREFIX/bin/rag-favorite" "$BIN_DIR/rag-favorite"
ln -sfn "$PREFIX/bin/rag-favorite-mcp" "$BIN_DIR/rag-favorite-mcp"
ln -sfn "$PREFIX/bin/rag-favorite-web" "$BIN_DIR/rag-favorite-web"

step "Initialize portable configuration"
CONFIG_FILE="$HOME/Library/Preferences/rag-favorite/config.toml"
if [ ! -f "$CONFIG_FILE" ]; then
  "$PREFIX/bin/rag-favorite" config init --force
fi
"$PREFIX/bin/rag-favorite" config validate
step "Prepare optional ingestion integration"
if ! "$VENV/bin/python" -c '
import subprocess
import sys

try:
    result = subprocess.run(sys.argv[2:], timeout=int(sys.argv[1]))
except subprocess.TimeoutExpired:
    print(f"Timed out after {sys.argv[1]} seconds: {sys.argv[2]}", file=sys.stderr)
    raise SystemExit(124)
raise SystemExit(result.returncode)
' 30 "$PREFIX/bin/rag-favorite" ingestion install \
    --project-dir "$INGESTION" \
    --render-launchd \
    --json; then
  echo "Warning: optional ingestion preparation failed; the core CLI remains installed." >&2
  echo "Retry with: rag-favorite ingestion install --project-dir \"$INGESTION\" --render-launchd" >&2
fi

PATH_UPDATED=false
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    if [ "$CONFIGURE_SHELL" = true ]; then
      SHELL_RC="$HOME/.zshrc"
      if [ "$ASSUME_YES" != true ]; then
        read -r -p "Add $BIN_DIR to PATH in $SHELL_RC? [Y/n] " shell_answer
        case "$shell_answer" in n|N|no|NO) CONFIGURE_SHELL=false ;; esac
      fi
      if [ "$CONFIGURE_SHELL" = true ]; then
        PATH_MARKER="# rag-favorite command line"
        PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
        if [ "$BIN_DIR" != "$HOME/.local/bin" ]; then
          PATH_LINE="export PATH=\"$BIN_DIR:\$PATH\""
        fi
        if [ ! -f "$SHELL_RC" ] || ! grep -F "$PATH_MARKER" "$SHELL_RC" >/dev/null 2>&1; then
          {
            echo
            echo "$PATH_MARKER"
            echo "$PATH_LINE"
          } >> "$SHELL_RC"
          PATH_UPDATED=true
        fi
      fi
    fi
    ;;
esac

echo
echo "rag-favorite $VERSION is installed."
if [ "$PATH_UPDATED" = true ]; then
  echo "PATH was added to ~/.zshrc. Run: source ~/.zshrc"
elif ! command -v rag-favorite >/dev/null 2>&1; then
  echo "For this terminal, run: export PATH=\"$BIN_DIR:\$PATH\""
fi
echo "Verify the CLI with: \"$PREFIX/bin/rag-favorite\" --version"
echo "Start the web UI with: rag-favorite-web"
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "FFmpeg is still required for media ingestion: brew install ffmpeg"
fi
echo "Configure credentials in: $INGESTION/.env"
echo "Core install and full RAG readiness are separate. Check external services with:"
echo "  rag-favorite setup status"
echo "  rag-favorite doctor"
echo "After configuring PostgreSQL/Telegram, enable workers with:"
echo "  rag-favorite ingestion install --project-dir \"$INGESTION\" --render-launchd --enable-services"
