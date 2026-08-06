# Video ingestion deployment

Video ingestion is an optional rag-favorite component. It uses the same
`config.toml`, database credentials and collection roots as the product core.
No source file should be edited to install it on another machine.

## Development checkout

```bash
cd /path/to/rag_favorite/ingestion
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
.venv/bin/python -m backend.ingestion.cli health
```

Leave path overrides empty unless the ingestion runtime must use a different
temporary directory or OpenClaw media directory. Collection roots and database
credentials come from `RAG_FAVORITE_CONFIG` or the standard XDG config path.

## Workers

The durable pipeline has separate commands for ingestion, author discovery,
batch notifications and cleanup:

```bash
.venv/bin/python -m backend.ingestion.cli worker
.venv/bin/python -m backend.ingestion.cli discovery-worker
.venv/bin/python -m backend.ingestion.cli batch-notification-worker
.venv/bin/python -m backend.ingestion.cli cleanup
```

The files in `deploy/` are systemd templates. Render every `@TOKEN@` described
in `deploy/README.md` before installing them. Phase 2 will add an idempotent CLI
renderer and installer.

## OpenClaw adapter

OpenClaw is not required by the ingestion core. When it is installed, the
optional plugin claims supported Telegram media and links, queues durable jobs,
and sends deterministic acknowledgements before the model replies.

For a local development checkout:

```bash
openclaw plugins install --link ./openclaw-plugin
openclaw plugins enable video-knowledge-ingest
openclaw plugins inspect video-knowledge-ingest --runtime --json
```

Set the plugin's `python` and `projectDir` configuration when the ingestion
virtual environment is not inside the checkout. The plugin derives the local
checkout path automatically and has no user-specific default path.

## Data and secrets

- Keep Telegram tokens and API keys in owner-only environment files.
- Keep Bilibili and Xiaohongshu cookies in separate mode-0600 files.
- Generated media is restricted to the configured OpenClaw media root and the
  ingestion test-fixture root.
- Temporary media is removed after verified staging; the cleanup command
  removes expired failed-job residue.
- PostgreSQL jobs survive worker and OpenClaw restarts.

## Migrations and feature gates

Apply ingestion migrations individually in numeric order. Never recursively
execute the `migrations/rollback/` directory. Author discovery and execution
flags default to `false`; installing schema does not enable network activity.

The author workflow performs a preview before any batch download. Platform
authentication, verification challenges and rate limits fail closed and do not
attempt to bypass platform controls.

## Operations

```bash
.venv/bin/python -m backend.ingestion.cli capabilities
.venv/bin/python -m backend.ingestion.cli security-audit
.venv/bin/python -m backend.ingestion.cli batch-metrics
.venv/bin/python -m backend.ingestion.cli release-observation --hours 24
```

See `AUTHOR_BATCH_OPERATIONS.md`, `BILIBILI_SESSION_MANAGER.md`,
`XIAOHONGSHU_SESSION_MANAGER.md`, and `KNOWLEDGE_BOUNDARIES.md` for component
runbooks.
