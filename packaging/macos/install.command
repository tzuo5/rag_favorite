#!/bin/bash
set -euo pipefail

VERSION="@VERSION@"
EXPECTED_ARCH="@ARCHITECTURE@"
WHEEL_NAME="@WHEEL_NAME@"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
PAYLOAD="$SCRIPT_DIR/payload"
PREFIX="$HOME/Library/Application Support/rag-favorite-cli"
BIN_DIR="$HOME/.local/bin"
ASSUME_YES=false
SKIP_BROWSER=false

usage() {
  echo "Usage: ./install.command [--prefix PATH] [--bin-dir PATH] [--yes] [--skip-browser]"
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
/usr/bin/shasum -a 256 -c SHA256SUMS

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
/usr/bin/ditto "$PAYLOAD/ingestion" "$INGESTION"
cp "$PAYLOAD/$WHEEL_NAME" "$PREFIX/wheels/$WHEEL_NAME"

VENV="$PREFIX/runtime/venv"
UV="$PAYLOAD/uv"
export UV_PYTHON_INSTALL_DIR="$PREFIX/runtime/python"
export UV_CACHE_DIR="$PREFIX/runtime/uv-cache"
"$UV" python install 3.12
"$UV" venv --python 3.12 "$VENV"
"$UV" pip install --python "$VENV/bin/python" "$PREFIX/wheels/$WHEEL_NAME[mcp]"
"$UV" pip install --python "$VENV/bin/python" --requirement "$INGESTION/requirements.txt"
if [ "$SKIP_BROWSER" != true ]; then
  "$VENV/bin/python" -m playwright install chromium
fi

ln -sfn "$VENV" "$INGESTION/.venv"

mkdir -p "$PREFIX/bin"
cat > "$PREFIX/bin/rag-favorite" <<'LAUNCHER'
#!/bin/bash
set -euo pipefail
APP_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
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
APP_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
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
APP_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
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

CONFIG_FILE="$HOME/Library/Preferences/rag-favorite/config.toml"
if [ ! -f "$CONFIG_FILE" ]; then
  "$PREFIX/bin/rag-favorite" config init --force
fi
"$PREFIX/bin/rag-favorite" config validate
"$PREFIX/bin/rag-favorite" ingestion install \
  --project-dir "$INGESTION" \
  --render-launchd \
  --json

echo
echo "rag-favorite $VERSION is installed."
echo "Add $BIN_DIR to PATH, then run: rag-favorite --help"
echo "Start the web UI with: rag-favorite-web"
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "FFmpeg is still required for media ingestion: brew install ffmpeg"
fi
echo "Configure credentials in: $INGESTION/.env"
echo "After configuring PostgreSQL/Telegram, enable workers with:"
echo "  rag-favorite ingestion install --project-dir \"$INGESTION\" --render-launchd --enable-services"
