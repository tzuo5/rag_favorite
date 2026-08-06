# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/).

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
