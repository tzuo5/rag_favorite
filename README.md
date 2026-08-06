# rag-favorite

Local-first document and media RAG with configurable collections, PostgreSQL +
pgvector retrieval, Ollama embeddings, a standard MCP server, and an optional
OpenClaw Telegram ingestion bridge.

The product core does not require OpenClaw. OpenClaw integration is an optional
adapter over the same local configuration and data.

## Architecture

```text
CLI / MCP / optional OpenClaw adapter
                 │
          rag_favorite core
       config · ingest · search
          │              │
 PostgreSQL + pgvector   Ollama
                 │
        local collection roots
```

## Phase 1 portable core

The repository now provides one installable Python package and command:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .

.venv/bin/rag-favorite config init
.venv/bin/rag-favorite config validate
.venv/bin/rag-favorite collection list
```

Configuration follows XDG conventions:

```text
~/.config/rag-favorite/config.toml
~/.config/rag-favorite/secrets.env
~/.local/share/rag-favorite/knowledge/
~/.cache/rag-favorite/
~/.local/state/rag-favorite/
```

Set `RAG_FAVORITE_CONFIG=/path/to/config.toml` or pass
`--config /path/to/config.toml` to use another profile. See
[`config/rag-favorite.example.toml`](config/rag-favorite.example.toml) and the
[Phase 1 design note](docs/phase-1-product-core.md).

## Collections

Collections are configured data, not hard-coded Python constants:

```toml
[[collections]]
key = "research"
name = "Research Notes"
path = "~/Documents/research"

[[collections]]
key = "recipes"
name = "Recipes"
path = "~/Documents/recipes"
template = "cooking"
```

The included eight-collection layout is a starter template. It can be replaced
with any valid collection keys and directories. Cooking uses the same document
index and search path; the older structured recipe writer remains an optional
compatibility extension.

## Index and search

After PostgreSQL/pgvector and Ollama are available and the credential file has
been configured:

```bash
rag-favorite init
rag-favorite doctor
rag-favorite ingest ~/Documents/research --collection research
rag-favorite search "deployment decision" --collection research
rag-favorite status --collection research
```

Database credentials may be supplied through `RAG_DATABASE_*`, standard `PG*`,
Docker `POSTGRES_*`, or the configured owner-only `secrets.env` file. Local mode
only accepts loopback PostgreSQL and Ollama endpoints.

## Components

- `src/rag_favorite/`: portable product configuration, database, embeddings,
  indexing, retrieval and unified CLI.
- `ingestion/`: video/audio download, transcription, author discovery and the
  optional OpenClaw Telegram plugin.
- `services/rag-mcp/`: MCP compatibility entrypoint and governed memory tools.
- `services/cooking-rag/`: optional structured recipe compatibility extension.
- `services/rag-postgres/`: PostgreSQL + pgvector development compose service.
- `services/ollama/`: Ollama development compose service.
- `migrations/core/`: canonical portable product migrations.
- `services/rag-app/migrations/`: legacy single-machine migration history.

The historical `services/rag-app/rag.py` path remains a thin compatibility
entrypoint and delegates to the installed `rag_favorite` package.

## OpenClaw boundary

The repository does not bundle OpenClaw. It contains only an optional plugin,
skill and example configuration. Standard local MCP packaging and automated
OpenClaw registration are scheduled for Phase 3 and Phase 4 respectively.

## Development checks

```bash
PYTHONPATH=src python -m pytest -q tests services/rag-app/tests
(cd ingestion && PYTHONPATH=../src python -m pytest -q tests)
PYTHONPATH=services/rag-mcp:src python -m unittest discover -s services/rag-mcp/tests
PYTHONPATH=services/cooking-rag:src python -m unittest discover -s services/cooking-rag/tests
node --test ingestion/openclaw-plugin/*.test.js
```

## Security

Do not commit Telegram tokens, database passwords, cookies, browser profiles,
database backups, personal knowledge documents, or generated OpenClaw state.
Generated credential files should remain mode `0600`. Retrieval results expose
collection-relative source labels rather than host filesystem paths.

Licensed under Apache-2.0. See [`LICENSE`](LICENSE).
