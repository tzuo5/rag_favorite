# systemd templates

The units in this directory are renderable templates. They intentionally do
not contain a user name or checkout path. Replace the following tokens before
installing a unit:

- `@INGESTION_DIR@`: absolute path to the installed ingestion directory;
- `@OPENCLAW_ENV@`: absolute path to OpenClaw's optional environment file;
- `@PRODUCT_DATA_DIR@`: rag-favorite XDG data directory;
- `@KNOWLEDGE_DIR@`: space-separated writable collection directories for the
  systemd `ReadWritePaths` directive;
- `@OPENCLAW_MEDIA_DIR@`: OpenClaw media directory.

`rag-favorite setup apply --render-systemd --ingestion-dir /absolute/path`
renders these units into the user systemd directory. Add `--enable-services`
only after reviewing the ingestion `.env`. The command never installs
system-wide units or invokes `sudo`.
