# Phase 4: optional OpenClaw integration

Phase 4 adds a guarded lifecycle for registering the packaged read-only MCP
server with an existing local OpenClaw installation. The repository still does
not bundle, download or install OpenClaw.

The integration calls OpenClaw's own `mcp` and `config validate` commands. It
does not directly invent schema fields or patch unrelated OpenClaw settings.

## Safe workflow

Install both products first:

```bash
pip install 'rag-favorite[mcp]'
rag-favorite setup apply
openclaw onboard
```

Then detect and preview before writing:

```bash
rag-favorite openclaw detect --json
rag-favorite openclaw plan --json
```

Register and run a real OpenClaw-to-MCP probe:

```bash
rag-favorite openclaw install
rag-favorite openclaw status --probe
```

Repeated installation is a no-op when the managed definition already matches.
Upgrades update definitions carrying the rag-favorite management marker while
preserving unknown OpenClaw fields. If another tool already owns an MCP server
named `rag-favorite`, plan reports `conflict` and install refuses to continue.
Use `--replace` only after reviewing that entry.

## Installed definition

OpenClaw receives one stdio server named `rag-favorite`. It launches the Python
environment where rag-favorite is installed and exposes only:

- `rag_search`;
- `rag_status`.

The definition contains the product config path and a management marker, but
no database password, Telegram token or other credential. Connection and
request timeouts are bounded, parallel calls are explicitly allowed for this
read-only server, and OpenClaw's tool filter is an allowlist of the two tools.

## Backups and rollback

Before every install, update, replacement or uninstall, rag-favorite validates
the active OpenClaw config and stores an owner-only snapshot plus SHA-256
metadata under:

```text
~/.local/state/rag-favorite/openclaw-backups/
```

The snapshot is a full OpenClaw config and may contain channel or gateway
credentials. The directory is mode `0700` and files are mode `0600`; never
publish them.

List records:

```bash
rag-favorite openclaw backups --json
```

Unregister only the managed server:

```bash
rag-favorite openclaw uninstall
```

OpenClaw intentionally blocks writes that appear to remove an unusually large
fraction of a small config. If its size-drop guard rejects removal,
rag-favorite safely disables the server instead and reports `action: disable`.
No unrelated setting is bypassed or deleted.

Restore an exact snapshot only when the current config still matches the
recorded post-operation hash:

```bash
rag-favorite openclaw restore \
  ~/.local/state/rag-favorite/openclaw-backups/<record>.metadata.json
```

Restore refuses if OpenClaw changed afterward. `--force` is available for an
intentional full-config rollback and can overwrite those later changes.

## Failure behavior

- Missing OpenClaw, missing product config or missing MCP extra fails before a
  write.
- Existing unmanaged entries require explicit replacement.
- Config snapshots are checksum verified.
- Registration uses a product-level lock to prevent concurrent rag-favorite
  operations.
- OpenClaw validates every mutation through its own schema.
- A failed config validation or MCP capability probe restores the exact
  pre-operation snapshot.
- Gateway runtime reload failure is reported separately; the validated config
  remains installed and is used on the next OpenClaw runtime start.

The optional Telegram ingestion plugin and skill remain separate adapters.
Phase 4 registers the standard RAG MCP server; it does not enable Telegram,
change channel policies or grant mutation tools.
