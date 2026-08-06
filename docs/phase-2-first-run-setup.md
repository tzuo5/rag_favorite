# Phase 2: first-run setup

Phase 2 turns the portable core into an idempotent, inspectable installation
workflow. It does not install OpenClaw and it never invokes `sudo`.

## Command contract

```bash
# Read-only preview
rag-favorite setup plan

# Use existing loopback PostgreSQL and Ollama, then apply schema migrations
rag-favorite setup apply

# Or start isolated local dependencies with Docker Compose
rag-favorite setup apply --start-services --pull-model

# Machine-readable readiness report
rag-favorite setup status --json
```

`rag-favorite setup` is an alias for `setup apply`. Re-running `apply` preserves
the existing configuration and credential file, recreates missing directories,
and skips migrations whose checksums and embedding dimensions match the
recorded history.

For an offline filesystem-only bootstrap, use `--skip-database`. This is useful
while preparing a host, but `setup status` will not report the installation as
ready until PostgreSQL, the canonical migrations, and the configured Ollama
model are available.

## Managed local services

`--start-services` requires Docker with the Compose plugin. The packaged Compose
definition binds PostgreSQL and Ollama only to the loopback ports selected in
the product config. Database credentials are passed to Compose through the
child process environment and are not written into the generated Compose file.

The generated runtime definition is stored under:

```text
~/.local/state/rag-favorite/runtime/compose.yaml
```

Existing databases and Ollama installations remain the default. Starting
containers is always explicit.

## Database migrations

Setup applies only migrations packaged from `migrations/core/`. It creates a
`rag_favorite_schema_migrations` ledger, verifies SHA-256 checksums, serializes
concurrent setup runs with a PostgreSQL advisory transaction lock, and records
the embedding dimension used to create the vector column. A changed historical
migration or incompatible dimension fails closed.

## Optional video ingestion services

On Linux, user-level systemd units can be rendered without root access:

```bash
rag-favorite setup apply --skip-database \
  --render-systemd \
  --ingestion-dir /absolute/path/to/rag_favorite/ingestion
```

The ingestion directory must contain `backend/` and a configured `.env` file;
enabling services also requires its `.venv`. Templates are written to
`~/.config/systemd/user/` by default. Add
`--enable-services` only when the environment has been reviewed and the
workers should start immediately. This runs `systemctl --user`; it never
modifies system-wide units.

OpenClaw remains optional. If no `--openclaw-env` is supplied, setup creates an
empty owner-only compatibility file. The `--openclaw-media-dir` default is
inside the rag-favorite XDG data directory.

## State and safety

Successful runs atomically record a credential-free summary at
`~/.local/state/rag-favorite/setup.json`. Config, credentials, generated runtime
files, and rendered units are owner-only. Setup never installs OS packages,
changes firewall rules, writes system services, or enables workers unless the
corresponding explicit flag is present.
