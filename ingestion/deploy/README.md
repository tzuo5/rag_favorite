# systemd templates

The units in this directory are renderable templates. They intentionally do
not contain a user name or checkout path. Replace the following tokens before
installing a unit:

- `@INGESTION_DIR@`: absolute path to the installed ingestion directory;
- `@OPENCLAW_ENV@`: absolute path to OpenClaw's optional environment file;
- `@PRODUCT_DATA_DIR@`: rag-favorite XDG data directory;
- `@KNOWLEDGE_DIR@`: parent directory containing configured collections;
- `@OPENCLAW_MEDIA_DIR@`: OpenClaw media directory.

The Phase 2 setup command will render and install these units. They should not
be copied directly into systemd before token substitution.
