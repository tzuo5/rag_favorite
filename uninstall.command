#!/bin/bash
set -euo pipefail

PREFIX="$HOME/Library/Application Support/rag-favorite-cli"
BIN_DIR="$HOME/.local/bin"
ASSUME_YES=false
PURGE_DATA=false
LABELS=(
  com.rag-favorite.video-ingestion-worker
  com.rag-favorite.video-author-discovery-worker
  com.rag-favorite.video-batch-notification-worker
  com.rag-favorite.video-ingestion-cleanup
  com.rag-favorite.xhs-session-manager
  com.rag-favorite.bilibili-session-manager
)

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="${2:?--prefix requires a path}"; shift 2 ;;
    --bin-dir) BIN_DIR="${2:?--bin-dir requires a path}"; shift 2 ;;
    --purge-data) PURGE_DATA=true; shift ;;
    --yes) ASSUME_YES=true; shift ;;
    --help|-h)
      echo "Usage: ./uninstall.command [--prefix PATH] [--bin-dir PATH] [--purge-data] [--yes]"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

case "$PREFIX" in
  ""|/|"$HOME") echo "Unsafe installation prefix: $PREFIX" >&2; exit 1 ;;
esac
case "$PREFIX:$BIN_DIR" in
  /*:/*) ;;
  *) echo "Installation and command paths must be absolute." >&2; exit 1 ;;
esac
if [ ! -f "$PREFIX/.rag-favorite-macos-install" ]; then
  echo "No managed rag-favorite macOS installation found at: $PREFIX" >&2
  exit 1
fi
if [ "$ASSUME_YES" != true ]; then
  echo "Application binaries under $PREFIX will be removed."
  if [ "$PURGE_DATA" = true ]; then
    echo "WARNING: --purge-data also deletes configuration, credentials, sessions, and knowledge files."
  else
    echo "Configuration, credentials, sessions, media, knowledge, and database data will be preserved."
  fi
  read -r -p "Continue? [y/N] " answer
  case "$answer" in y|Y|yes|YES) ;; *) echo "Cancelled."; exit 0 ;; esac
fi

DOMAIN="gui/$(id -u)"
for label in "${LABELS[@]}"; do
  launchctl bootout "$DOMAIN/$label" >/dev/null 2>&1 || true
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done

for link in rag-favorite rag-favorite-mcp rag-favorite-web; do
  target="$BIN_DIR/$link"
  if [ -L "$target" ] && [ "$(readlink "$target")" = "$PREFIX/bin/$link" ]; then
    rm -f "$target"
  fi
done

INGESTION="$PREFIX/ingestion"
if [ -f "$INGESTION/.managed-files" ]; then
  while IFS= read -r relative; do
    case "$relative" in ""|/*|*".."*) continue ;; esac
    rm -f "$INGESTION/$relative"
  done < "$INGESTION/.managed-files"
  rm -f "$INGESTION/.managed-files"
fi
rm -f "$INGESTION/.venv"
rm -rf "$PREFIX/runtime" "$PREFIX/wheels" "$PREFIX/bin"
rm -f "$PREFIX/.rag-favorite-macos-install"

if [ "$PURGE_DATA" = true ]; then
  rm -rf \
    "$PREFIX" \
    "$HOME/Library/Preferences/rag-favorite" \
    "$HOME/Library/Application Support/rag-favorite" \
    "$HOME/Library/Caches/rag-favorite"
  echo "rag-favorite and its local application data were removed. External PostgreSQL data was not deleted."
else
  echo "rag-favorite application binaries were removed; local data was preserved."
  echo "Run again with --purge-data only if permanent local deletion is intended."
fi
