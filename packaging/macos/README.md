# rag-favorite @VERSION@ for macOS (@ARCHITECTURE@)

This archive installs the same rag-favorite CLI, read-only MCP server, web UI,
media ingestion pipeline, Telegram adapter, and guarded OpenClaw integration as
the source release. It includes a reviewed uv @ARCHITECTURE@ bootstrap binary so
the installer can provision an isolated Python 3.12 runtime without modifying
Apple's system Python.

The bundled `uv` executable is redistributed under its upstream Apache-2.0 or
MIT terms; both license texts are included in `payload/`.

## Install

1. Extract the archive.
2. Right-click `install.command` and choose **Open**. The release is not Apple
   notarized, so Finder may require this one-time confirmation.
3. Review the paths and approve installation. Python packages and Chromium are
   downloaded during first setup.
4. Add `~/.local/bin` to your shell `PATH` if it is not already present.

```bash
./install.command
rag-favorite --version
rag-favorite-web
```

Install FFmpeg separately for audio/video processing:

```bash
brew install ffmpeg
```

PostgreSQL + pgvector, Ollama, Telegram credentials, and OpenClaw remain
operator-controlled external services. Edit the private `.env` path printed by
the installer, then preview and enable the launchd workers:

```bash
rag-favorite ingestion plan --render-launchd --enable-services --with-openclaw
rag-favorite ingestion install --render-launchd --enable-services --with-openclaw
```

OpenClaw registration is optional and never installs or restarts OpenClaw.

## Verify the download

From the directory containing the release archive and `.sha256` file:

```bash
shasum -a 256 -c rag-favorite-@VERSION@-macos-@ARCHITECTURE@.tar.gz.sha256
```

The installer verifies every bundled payload file again before making changes.

## Uninstall

Double-click or run `uninstall.command`. The default preserves configuration,
credentials, sessions, media, knowledge files, and database data:

```bash
./uninstall.command
```

Permanent local application-data deletion requires the explicit
`--purge-data` option. External PostgreSQL data is never deleted.
