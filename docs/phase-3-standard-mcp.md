# Phase 3: standard local MCP packaging

Phase 3 makes the read-only RAG interface available from the installed Python
wheel. It removes checkout paths, service-specific virtual environments and
the legacy `rag.py` subprocess from the standard MCP path.

OpenClaw is not required. Any local MCP client that can launch a stdio server
can use the same entrypoint and product configuration.

## Install and verify

```bash
python3 -m venv .venv
.venv/bin/pip install 'rag-favorite[mcp]'

.venv/bin/rag-favorite setup apply
.venv/bin/rag-favorite mcp inspect
.venv/bin/rag-favorite mcp smoke
```

The dedicated server command is:

```bash
.venv/bin/rag-favorite-mcp
```

It uses MCP stdio transport. It does not bind a TCP port and must be launched
by the client rather than run in an interactive terminal.

## Client configuration

Generate a client-neutral `mcpServers` fragment:

```bash
rag-favorite mcp config
```

Example output:

```json
{
  "mcpServers": {
    "rag-favorite": {
      "command": "/absolute/path/to/venv/bin/python",
      "args": ["-m", "rag_favorite.mcp_server"],
      "env": {
        "RAG_FAVORITE_CONFIG": "/absolute/path/to/config.toml"
      }
    }
  }
}
```

The Python interpreter path is absolute but its virtual-environment symlink is
preserved, so the child process uses the environment where the MCP extra was
installed. The fragment contains no database password or other credential.

To write a standalone fragment instead of printing it:

```bash
rag-favorite mcp config --output ./rag-favorite.mcp.json
```

Existing files are never overwritten unless `--force` is supplied. Phase 3
does not merge this fragment into a client's configuration. Automated
OpenClaw discovery and registration belong to Phase 4.

## Stable tool contract

The packaged server exposes exactly two tools:

- `rag_status`: inspect configured collection and index health;
- `rag_search`: search one explicit collection, with optional explicit
  cross-collection search.

Both tools are read-only. Search limits are bounded from 1 to 10, queries are
bounded to 2,000 characters, collection names come from the active product
configuration, and results expose collection-relative source paths. Backend
exceptions are converted into safe error envelopes without filesystem paths,
database details or tracebacks.

Governed memory mutation tools and the structured Cooking writer remain in
their existing compatibility services. They are deliberately not included in
the standard read-only server because their schemas and least-privilege roles
are not part of the portable core migration history.

## Compatibility boundary

`services/rag-mcp/` remains available for the existing deployment until its
client configuration is migrated. New clients should use
`rag-favorite-mcp`. Keeping the entrypoints separate makes the Phase 3 tool set
deterministic and avoids silently granting write capabilities to a generic MCP
client.
