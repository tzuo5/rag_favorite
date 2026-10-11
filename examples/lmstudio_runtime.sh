#!/usr/bin/env bash
# Control the prepared user-owned LM Studio + Phase 1 PostgreSQL runtime.
set -euo pipefail
rag_lm_repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
rag_lm_runtime="$rag_lm_repo/.runtime/phase1"
rag_lm_cli="${RAG_LMS_PATH:-$HOME/.lmstudio/bin/lms}"
rag_lm_python="$rag_lm_repo/.venv/bin/python"
rag_lm_pg="$rag_lm_runtime/postgres/usr/lib/postgresql/18/bin/pg_ctl"
export LD_LIBRARY_PATH="$rag_lm_runtime/postgres/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# pg_ctl parses this option string separately; quote paths containing spaces.
rag_lm_pg_options="$("$rag_lm_python" - "$rag_lm_runtime" <<'PY'
import shlex, sys
print(shlex.join(["-p", "55432", "-h", "127.0.0.1", "-k", sys.argv[1]]))
PY
)"

case "${1:-status}" in
    start)
        if ! "$rag_lm_pg" -D "$rag_lm_runtime/pgdata" status >/dev/null 2>&1; then
            "$rag_lm_pg" -D "$rag_lm_runtime/pgdata" -l "$rag_lm_runtime/postgres.log" \
                -o "$rag_lm_pg_options" -w start
        fi
        "$rag_lm_cli" daemon up
        if ! "$rag_lm_cli" ps --json | "$rag_lm_python" -c \
            'import json,sys; raise SystemExit(0 if any(m.get("identifier")=="text-embedding-qwen3-embedding-0.6b" for m in json.load(sys.stdin)) else 1)'; then
            "$rag_lm_cli" load text-embedding-qwen3-embedding-0.6b --gpu off --context-length 4096
        fi
        "$rag_lm_cli" server start --bind 127.0.0.1 --port 1234
        ;;
    stop)
        "$rag_lm_cli" server stop
        "$rag_lm_cli" unload text-embedding-qwen3-embedding-0.6b
        if "$rag_lm_pg" -D "$rag_lm_runtime/pgdata" status >/dev/null 2>&1; then
            "$rag_lm_pg" -D "$rag_lm_runtime/pgdata" -m fast -w stop
        fi
        ;;
    status)
        "$rag_lm_pg" -D "$rag_lm_runtime/pgdata" status
        "$rag_lm_cli" server status
        "$rag_lm_cli" ps --json
        ;;
    *) echo "Usage: bash examples/lmstudio_runtime.sh [start|stop|status]" >&2; exit 2 ;;
esac
