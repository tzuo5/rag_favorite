# Phase 5: portable media ingestion lifecycle

Phase 5 turns the repository's optional video/audio pipeline and OpenClaw
Telegram adapter into an inspectable installation lifecycle. It does not bundle
OpenClaw, configure a Telegram channel, invent credentials, or add mutation
tools to the standard read-only MCP server.

## Detect and preview

Run detection without writing files:

```bash
rag-favorite ingestion detect --json
```

When the command is not run from the repository root, select the checked-out
ingestion source explicitly:

```bash
rag-favorite ingestion detect \
  --project-dir /absolute/path/to/rag_favorite/ingestion
```

Preview every requested operation before applying it:

```bash
rag-favorite ingestion plan \
  --install-dependencies \
  --render-systemd \
  --with-openclaw
```

The plan reports filesystem, network, user-service, and OpenClaw changes. It
does not create `.env`, a virtual environment, systemd units, or registrations.

## Install

Apply the reviewed plan:

```bash
rag-favorite ingestion install \
  --install-dependencies \
  --render-systemd \
  --with-openclaw
```

The lifecycle:

1. creates an owner-only `ingestion/.env` when it is absent;
2. preserves an existing `.env` byte-for-byte and restricts its mode to `0600`;
3. creates private cache, staging, media, and writable collection directories;
4. optionally creates `ingestion/.venv` and installs the declared ingestion
   requirements;
5. optionally renders hardened user-level systemd services on Linux or
   owner-only launchd jobs on macOS;
6. optionally links and probes the repository's OpenClaw plugin.

Dependency installation is always explicit because it performs network access
and includes media/browser packages. Service activation is a second explicit
gate:

```bash
rag-favorite ingestion plan \
  --install-dependencies \
  --render-systemd \
  --enable-services

rag-favorite ingestion install \
  --install-dependencies \
  --render-systemd \
  --enable-services
```

On Linux, `--enable-services` requires `--render-systemd`. Both operate only
on the current user's systemd instance and never invoke `sudo`.

On macOS, use the equivalent launchd gate:

```bash
rag-favorite ingestion plan --render-launchd --enable-services
rag-favorite ingestion install --render-launchd --enable-services
```

The jobs are written to `~/Library/LaunchAgents` and remain scoped to the
current GUI user. See the [macOS guide](macos.md).

## Credentials and readiness

The generated `.env` contains empty values for:

```dotenv
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USER_IDS=
OPENAI_API_KEY=
```

Add secrets locally and keep the file mode `0600`. The Telegram values are
required only for Telegram claims and outbound notifications. The OpenAI values
are optional; deterministic transcript preservation remains available without
metadata enrichment.

Inspect readiness without exposing values:

```bash
rag-favorite ingestion status --json
rag-favorite ingestion status --with-openclaw --json
```

Status reports whether Telegram credentials are configured as one boolean; it
never prints tokens, user IDs, cookies, database credentials, or absolute
knowledge-document names.

## Guarded OpenClaw plugin registration

`--with-openclaw` requires an existing, onboarded OpenClaw installation. The
lifecycle uses OpenClaw's official `plugins install --link`, `config set`,
`config validate`, and `plugins inspect --runtime` commands.

Before mutation, rag-favorite validates and snapshots the owner-only OpenClaw
configuration under its managed backup directory. Installation then verifies:

- the linked plugin source belongs to the selected ingestion checkout;
- the configured Python belongs to that checkout's virtual environment;
- the read-only `video_ingestion_capabilities` tool is loaded;
- the bounded `/video_*` command set is present;
- the three required hooks load without error diagnostics.

The plugin needs conversation access to claim Telegram media before the model
reply and to suppress duplicate responses after deterministic handling. Phase 5
grants that permission only at:

```text
plugins.entries.video-knowledge-ingest.hooks.allowConversationAccess
```

It does not enable Telegram, set a bot token, expand channel allowlists, change
gateway binding/authentication, or modify any other plugin. An existing plugin
with the same ID but a different source fails closed as an unmanaged collision.
A failed validation or runtime contract probe uninstalls a new link and restores
the exact configuration snapshot.

Restart the OpenClaw gateway after a successful plugin install, update, or
uninstall. Phase 5 reports `restart_required: true` rather than restarting a
live gateway without operator approval.

## Uninstall and preservation

Disable only the managed integrations:

```bash
rag-favorite ingestion uninstall --with-openclaw
rag-favorite ingestion uninstall --disable-services
```

Uninstall intentionally preserves `.env`, `.venv`, knowledge documents,
database data, media, session state, and backups. Removing those assets is a
separate operator decision. Unmanaged OpenClaw plugin collisions are never
removed unless `--force` is explicitly supplied.

## Legacy governed-memory boundary

The historical `services/rag-app/migrations/phase5.1` through `phase5.3`
records describe a private deployment's governed-memory mutation model. They
remain compatibility history and are not applied by this portable Phase 5.
The packaged MCP surface continues to expose only `rag_search` and `rag_status`.
