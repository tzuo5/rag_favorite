# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [1.2.0] - 2026-08-06

### Added

- Downloadable macOS release archives for Apple Silicon and Intel, each built
  and installation-tested on its matching GitHub-hosted Mac runner.
- A checksummed, architecture-guarded installer with a bundled reviewed `uv`
  bootstrap, isolated Python 3.12 environment, complete ingestion dependencies,
  Chromium provisioning, CLI/MCP/web launchers, and data-preserving uninstaller.
- Native per-user `launchd` rendering and lifecycle support for ingestion,
  discovery, notification, cleanup, Xiaohongshu, and Bilibili jobs.
- macOS launchd recovery paths for Bilibili and Xiaohongshu session refreshes,
  including a direct Playwright probe without Linux-only Xvfb.
- macOS installation, Gatekeeper, external-service, verification, upgrade, and
  removal documentation.

### Security

- macOS bundles verify every embedded payload before installation and publish
  separate SHA-256 checksum assets.
- Install and uninstall scripts reject broad target paths, avoid replacing
  unmanaged command files, keep user services unprivileged, and preserve data
  unless `--purge-data` is explicitly requested.
- The pinned macOS `uv` 0.11.16 binaries are verified against upstream release
  digests before packaging.
- macOS installs the MCP SDK's local-stdio dependency set without its unused
  HTTP OAuth crypto extra, avoiding unsupported Intel source builds instead of
  downgrading to an older cryptography release.

## [1.1.0] - 2026-08-06

### Added

- Portable `ingestion detect`, `plan`, `install`, `status`, and `uninstall`
  lifecycle commands.
- Owner-only, secret-free ingestion environment bootstrap with portable product
  paths and explicit dependency installation.
- Guarded OpenClaw Telegram ingestion plugin registration using the official
  plugin CLI, configuration snapshots, runtime contract probing, rollback, and
  unmanaged collision protection.
- Phase 5 operator guide covering service activation, credentials, permissions,
  restart behavior, and preserved data.

### Changed

- Product and plugin versions advance to 1.1.0.
- The optional Playwright client advances to 1.62.0 after compatibility tests.
- OpenClaw backup metadata can identify either the MCP server or the ingestion
  plugin while remaining compatible with existing restore records.

## [1.0.0] - 2026-08-06

First stable local-first release.

### Added

- Installable `rag_favorite` package and unified `rag-favorite` CLI.
- XDG-based configuration, named collections, safe credential loading, and
  portable PostgreSQL/pgvector migrations.
- Idempotent first-run setup with plan, apply, status, service startup, model
  pulling, filesystem-only mode, and optional systemd rendering.
- Standard read-only MCP stdio server with `rag_search` and `rag_status` tools,
  client configuration generation, introspection, and protocol smoke testing.
- Guarded OpenClaw lifecycle with detection, planning, collision protection,
  locking, owner-only checksummed backups, validation, probing, rollback,
  targeted uninstall, and explicit restore.
- Video and audio ingestion, transcription, author discovery, batch workflows,
  session recovery, Telegram delivery, and optional OpenClaw plugin.
- PostgreSQL, Ollama, legacy MCP, and structured cooking compatibility services.
- Bilingual project homepage, CI, release builds, contribution guidance,
  security policy, and GitHub collaboration templates.

### Security

- Local mode rejects non-loopback database and embedding endpoints.
- Retrieval responses use collection-relative source labels instead of host
  filesystem paths.
- Standard MCP and OpenClaw registration expose only the read-only tool set.

[1.0.0]: https://github.com/tzuo5/rag_favorite/releases/tag/v1.0.0
[1.1.0]: https://github.com/tzuo5/rag_favorite/releases/tag/v1.1.0
[1.2.0]: https://github.com/tzuo5/rag_favorite/releases/tag/v1.2.0
