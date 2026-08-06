# Security policy

rag-favorite processes private documents, platform sessions, and service
credentials. Security reports are welcome and should be handled privately.

## Supported versions

| Version | Supported |
| --- | --- |
| 1.x | Yes |
| 0.x | No |

## Report a vulnerability

Use the repository's
[private vulnerability reporting](https://github.com/tzuo5/rag_favorite/security/advisories/new)
form. Do not include secrets, private knowledge documents, cookies, or production
database contents. Please provide:

- the affected version and component;
- a minimal reproduction or proof of concept;
- the impact and any known mitigations;
- whether the issue is already public.

Please allow a reasonable remediation window before disclosure. Maintainers will
acknowledge a valid report, assess severity, coordinate a fix, and credit the
reporter unless anonymity is requested.

## Security boundaries

- Product mode restricts PostgreSQL and Ollama endpoints to loopback addresses.
- Generated credential files and OpenClaw backups are owner-only.
- The packaged MCP server exposes only read-only retrieval and status tools.
- OpenClaw is an optional external dependency. Registration changes only the
  named MCP entry and validates, backs up, probes, and rolls back configuration.
- Media platform adapters process untrusted remote metadata and should run with
  least privilege in an isolated environment.
- macOS assets publish SHA-256 sidecars and verify their embedded payload before
  installation. Current archives are not Apple-notarized; use Finder's scoped
  **Open** confirmation and never disable Gatekeeper globally.

Users are responsible for access control on their host, database, OpenClaw
gateway, Telegram bot, collection directories, and backups.
