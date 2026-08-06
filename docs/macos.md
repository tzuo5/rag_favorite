# macOS release guide

rag-favorite publishes separate, installation-tested archives for Apple Silicon
and Intel Macs. Both contain the same CLI, read-only MCP server, media pipeline,
Telegram/OpenClaw adapter, web UI, and launchd service definitions as the source
release.

## Choose and verify an archive

Check the architecture:

```bash
uname -m
```

Download the matching archive and checksum from the
[latest GitHub Release](https://github.com/tzuo5/rag_favorite/releases/latest):

| `uname -m` | Release asset |
| --- | --- |
| `arm64` | `rag-favorite-1.2.0-macos-arm64.tar.gz` |
| `x86_64` | `rag-favorite-1.2.0-macos-x86_64.tar.gz` |

Verify before extraction:

```bash
shasum -a 256 -c rag-favorite-1.2.0-macos-arm64.tar.gz.sha256
```

The installer performs a second SHA-256 verification over every embedded
payload file. It also refuses to run when the archive architecture differs from
the current Mac.

## Install

Extract the archive, then right-click `install.command` and choose **Open**.
The project does not currently hold an Apple Developer ID certificate, so the
archive is not notarized and Finder may require this one-time Gatekeeper
confirmation. Do not disable Gatekeeper globally.

The installer:

1. installs under `~/Library/Application Support/rag-favorite-cli`;
2. uses the bundled, checksum-pinned `uv` binary to provision Python 3.12;
3. creates an isolated environment and installs the core, MCP, media, browser,
   and platform dependencies;
4. installs Chromium for Playwright session workflows;
5. creates `rag-favorite`, `rag-favorite-mcp`, and `rag-favorite-web` links in
   `~/.local/bin`;
6. initializes private configuration and ingestion environment files;
7. renders owner-only launchd jobs without activating them.

Third-party packages and Chromium are downloaded during installation. Use
`--skip-browser` only if Xiaohongshu/Bilibili browser-session features are not
required.

The product MCP endpoint is local stdio only. The archive therefore installs
the MCP SDK's stdio runtime set without its unused HTTP OAuth crypto extra. This
keeps both Mac architectures on supported wheels; it does not remove MCP tools,
the protocol client, or OpenClaw compatibility. CI runs a real MCP handshake on
both architectures.

```bash
./install.command
export PATH="$HOME/.local/bin:$PATH"
rag-favorite --version
rag-favorite ingestion detect --json
```

## External services

The archive intentionally does not install or silently configure machine-wide
services. Install FFmpeg with Homebrew for audio/video processing:

```bash
brew install ffmpeg
```

PostgreSQL + pgvector and Ollama must be reachable on loopback. Docker Desktop,
Colima, native Homebrew services, or another local runtime can provide them.
OpenClaw remains optional and must already be onboarded before registration.

Add Telegram, database, and optional OpenAI credentials to the private `.env`
path printed by the installer. Never commit that file.

## launchd workers

Preview before activation:

```bash
rag-favorite ingestion plan \
  --render-launchd \
  --enable-services \
  --with-openclaw
```

Apply only after the database and credentials are ready:

```bash
rag-favorite ingestion install \
  --render-launchd \
  --enable-services \
  --with-openclaw
```

This installs per-user jobs in `~/Library/LaunchAgents`; it does not use `sudo`.
Logs are written beneath
`~/Library/Application Support/rag-favorite/logs`. OpenClaw registration still
uses validation, private backups, runtime probes, and rollback, and reports when
a gateway restart is required instead of restarting it.

Disable services while preserving files:

```bash
rag-favorite ingestion uninstall --disable-services --with-openclaw
```

## Upgrade and uninstall

Running a newer archive's installer upgrades managed source files and the
isolated runtime while preserving `.env`, secrets, sessions, media, knowledge,
configuration, and external database data.

`uninstall.command` removes only managed application binaries by default and
preserves local data. `--purge-data` is deliberately separate and permanently
deletes local configuration, credentials, session state, media, and knowledge
directories. It never deletes external PostgreSQL data.
