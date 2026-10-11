#!/usr/bin/env bash
# Control only the already-prepared, user-owned Phase 1 validation runtime.
set -euo pipefail
rag_phase1_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.runtime/phase1"
if "$(dirname "$rag_phase1_root")/../.venv/bin/python" - "$rag_phase1_root/config.toml" <<'PY'
import sys, tomllib
from pathlib import Path
config = tomllib.loads(Path(sys.argv[1]).read_text())
raise SystemExit(0 if config.get("embedding", {}).get("backend") == "lmstudio" else 1)
PY
then
    exec bash "$(dirname "${BASH_SOURCE[0]}")/lmstudio_runtime.sh" "${1:-status}"
fi
rag_phase1_pg="$rag_phase1_root/postgres/usr/lib/postgresql/18/bin/pg_ctl"
rag_phase1_ollama="$rag_phase1_root/ollama/bin/ollama"
export LD_LIBRARY_PATH="$rag_phase1_root/postgres/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

ollama_owned() {
    [[ -f "$rag_phase1_root/ollama.pid" ]] || return 1
    read -r rag_phase1_pid < "$rag_phase1_root/ollama.pid"
    [[ "$rag_phase1_pid" =~ ^[0-9]+$ ]] || return 1
    [[ "$(readlink "/proc/$rag_phase1_pid/exe" 2>/dev/null || true)" == "$rag_phase1_ollama" ]]
}

case "${1:-status}" in
    start)
        [[ -x "$rag_phase1_pg" && -x "$rag_phase1_ollama" ]] || {
            echo "Phase 1 runtime binaries are missing; see the Phase 1 implementation record." >&2
            exit 1
        }
        if ! "$rag_phase1_pg" -D "$rag_phase1_root/pgdata" status >/dev/null 2>&1; then
            "$rag_phase1_pg" -D "$rag_phase1_root/pgdata" -l "$rag_phase1_root/postgres.log" \
                -o "-p 55432 -h 127.0.0.1 -k $rag_phase1_root" -w start
        fi
        if ! ollama_owned; then
            if curl -fsS --max-time 1 http://127.0.0.1:11435/api/version >/dev/null 2>&1; then
                echo "Port 11435 is occupied by an unregistered process; no process was stopped." >&2
                exit 1
            fi
            # A separate session survives the launching terminal/agent shell.
            setsid --fork bash -c '
                echo "$$" > "$1/ollama.pid"
                exec env OLLAMA_HOST=127.0.0.1:11435 OLLAMA_MODELS="$1/models" \
                    OLLAMA_NO_CLOUD=1 "$2" serve
            ' bash "$rag_phase1_root" "$rag_phase1_ollama" \
                < /dev/null > "$rag_phase1_root/ollama.log" 2>&1
            for rag_phase1_attempt in {1..20}; do
                if curl -fsS --max-time 1 http://127.0.0.1:11435/api/version >/dev/null 2>&1; then
                    break
                fi
                sleep 0.5
            done
        fi
        curl -fsS --max-time 2 http://127.0.0.1:11435/api/version
        ;;
    stop)
        if ollama_owned; then
            kill "$rag_phase1_pid"
            rm -f "$rag_phase1_root/ollama.pid"
        fi
        if "$rag_phase1_pg" -D "$rag_phase1_root/pgdata" status >/dev/null 2>&1; then
            "$rag_phase1_pg" -D "$rag_phase1_root/pgdata" -m fast -w stop
        fi
        ;;
    status)
        "$rag_phase1_pg" -D "$rag_phase1_root/pgdata" status
        ollama_owned || { echo "Owned Ollama process is not running." >&2; exit 1; }
        curl -fsS --max-time 2 http://127.0.0.1:11435/api/version
        ;;
    *) echo "Usage: bash examples/phase1_runtime.sh [start|stop|status]" >&2; exit 2 ;;
esac
