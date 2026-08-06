# Contributing to rag-favorite

Thank you for helping improve rag-favorite. Changes should preserve its core
contract: local-first operation, portable configuration, read-only public MCP
tools, and guarded optional integrations.

## Development setup

```bash
git clone https://github.com/tzuo5/rag_favorite.git
cd rag_favorite
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[mcp,ingestion,dev]"
```

Python 3.11 or newer is required. Node.js is only needed for the optional
OpenClaw ingestion plugin tests. Integration tests that need PostgreSQL are
skipped unless their documented environment variables are set.

## Checks

Run the same release checks used by CI:

```bash
make check
```

Focused commands are also available:

```bash
make lint
make test-core
make test-ingestion
make test-compat
make test-node
make test-macos
make build
```

New behavior needs tests. Security-sensitive setup or OpenClaw changes should
cover failure, rollback, idempotency, file permissions, and unmanaged-state
collision cases.

## Pull requests

1. Open a focused issue for material behavior changes.
2. Branch from `main` and keep the diff scoped to one concern.
3. Update documentation and `CHANGELOG.md` when a public contract changes.
4. Run `make check` and include any intentionally skipped integration checks in
   the pull-request description.
5. Never commit local configuration, credentials, cookies, user documents,
   generated OpenClaw state, or database dumps.

Use short, imperative commit subjects. Pull requests are squash-merged after CI
passes and the security boundary is understood.

## Compatibility promises

- The `rag-favorite` command and documented subcommands are public interfaces.
- `rag_search` and `rag_status` are the stable packaged MCP tools.
- Database changes must be idempotent and include an explicit migration path.
- OpenClaw remains optional and must never be installed or reconfigured outside
  the named `rag-favorite` MCP registration.
- macOS bundle changes must pass both Apple Silicon and Intel installation
  smoke tests before release.

For vulnerabilities, do not open a public issue; follow [SECURITY.md](SECURITY.md).
