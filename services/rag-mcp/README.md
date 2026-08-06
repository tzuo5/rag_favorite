# Legacy MCP compatibility service

This directory preserves the original deployment entrypoint, governed-memory
extensions and their historical tests. It may expose additional mutation tools
when a dedicated runtime credential is installed.

New installations should use the packaged, deterministic read-only server:

```bash
pip install 'rag-favorite[mcp]'
rag-favorite mcp smoke
rag-favorite mcp config
```

See `docs/phase-3-standard-mcp.md`. The compatibility service will remain
unchanged until a separately reviewed migration covers its database roles and
client allowlists.
