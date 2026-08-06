# Phase 1: portable product core

Phase 1 turns the original deployment checkout into one installable Python
product while preserving old service paths as compatibility entrypoints.

## Stable interfaces

- Package: `rag_favorite`
- CLI: `rag-favorite`
- Configuration: `$XDG_CONFIG_HOME/rag-favorite/config.toml`
- Credentials: `$XDG_CONFIG_HOME/rag-favorite/secrets.env`
- Product data: `$XDG_DATA_HOME/rag-favorite`
- Cache: `$XDG_CACHE_HOME/rag-favorite`
- State: `$XDG_STATE_HOME/rag-favorite`

The XDG variables are optional and use the standard directories under the
current user's home when absent.

## Configuration rules

Collections are data. Each `[[collections]]` entry supplies a stable key,
display name, source root, optional template and read-only flag. The default
eight-collection layout is only a template and can be replaced entirely.

Database credentials stay outside `config.toml`. The core accepts
`RAG_DATABASE_*`, standard `PG*`, Docker `POSTGRES_*`, or the configured local
credential file. PostgreSQL and Ollama are loopback-only in the local product
profile.

## Compatibility boundary

`services/rag-app/rag.py`, the ingestion adapters and the existing MCP server
now load the package configuration. The specialized recipe writer remains as
an optional compatibility extension, while ordinary Cooking indexing and
search use the same document RAG as every other collection.

Historical deployment SQL and reports remain under
`services/rag-app/migrations`; new product migrations live under
`migrations/core`.
