#!/usr/bin/env bash
# User-owned services and remote Terminal access; no sudo.
set -euo pipefail
rag_video_repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export RAG_FAVORITE_CONFIG="${RAG_FAVORITE_CONFIG:-$rag_video_repo/.runtime/phase1/config.toml}"
export RAG_VIDEO_CONFIG="${RAG_VIDEO_CONFIG:-$rag_video_repo/.runtime/videorag/video.toml}"
case "${1:-status}" in
    install)
        "$rag_video_repo/.venv/bin/python" "$rag_video_repo/examples/install_video_services.py"
        ;;
    uninstall)
        "$rag_video_repo/.venv/bin/python" "$rag_video_repo/examples/install_video_services.py" --uninstall
        ;;
    favorites-schedule)
        shift
        exec "$rag_video_repo/.venv/bin/python" "$rag_video_repo/examples/install_favorites_timer.py" "$@"
        ;;
    following-schedule)
        shift
        exec "$rag_video_repo/.venv/bin/python" "$rag_video_repo/examples/install_following_timer.py" "$@"
        ;;
    following-status)
        "$rag_video_repo/.venv/bin/python" -m rag_favorite.video_cli following-status --json
        ;;
    favorites-nightly-status)
        systemctl --user --no-pager --lines=4 status rag-favorite-favorites.timer rag-favorite-favorites.service || true
        "$rag_video_repo/.venv/bin/python" -m rag_favorite.video_cli favorites-status
        ;;
    start)
        systemctl --user start rag-favorite-runtime.service rag-favorite-imagebind.service rag-favorite-video-worker.service rag-favorite-summary-worker.service
        ;;
    stop)
        systemctl --user stop rag-favorite-video-worker.service rag-favorite-summary-worker.service rag-favorite-imagebind.service
        ;;
    restart)
        systemctl --user restart rag-favorite-imagebind.service rag-favorite-video-worker.service rag-favorite-summary-worker.service
        ;;
    status)
        systemctl --user --no-pager --lines=4 status rag-favorite-runtime.service rag-favorite-imagebind.service rag-favorite-video-worker.service rag-favorite-summary-worker.service || true
        "$rag_video_repo/.venv/bin/python" -m rag_favorite.video_cli status
        "$rag_video_repo/.venv/bin/python" -m rag_favorite.video_cli summary-status
        ;;
    logs)
        journalctl --user -u rag-favorite-imagebind -u rag-favorite-video-worker -u rag-favorite-summary-worker -n 60 --no-pager
        ;;
    progress)
        cat "$rag_video_repo/docs/implementation/autonomous-progress.md"
        ;;
    report)
        "$rag_video_repo/.venv/bin/python" "$rag_video_repo/examples/update_video_sample_report.py"
        ;;
    cli)
        shift
        exec "$rag_video_repo/.venv/bin/python" -m rag_favorite.video_cli "$@"
        ;;
    mcp)
        exec "$rag_video_repo/.venv/bin/python" -m rag_favorite.video_mcp
        ;;
    *) echo "Usage: video_runtime.sh [install|uninstall|start|stop|restart|status|logs|progress|report|favorites-schedule ...|favorites-nightly-status|following-schedule ...|following-status|cli ...|mcp]" >&2; exit 2 ;;
esac
