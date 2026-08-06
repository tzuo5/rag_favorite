# Telegram video knowledge ingestion

The OpenClaw plugin is the only Telegram update consumer. It claims supported
inbound messages, queues durable PostgreSQL jobs and returns the immediate
acknowledgement. The systemd worker performs media processing and sends results.

## Commands

```bash
cd /home/ubuntu/AI-Video-Transcriber
systemctl --user start video-ingestion-worker.service
systemctl --user stop video-ingestion-worker.service
systemctl --user restart video-ingestion-worker.service
journalctl --user -u video-ingestion-worker.service -f
.venv/bin/python -m backend.ingestion.cli health
.venv/bin/python -m backend.ingestion.cli status --user-id 1000000001
```

Update code on the existing feature branch, install changed requirements, run
tests, then restart the worker. OpenClaw only needs a restart when plugin code or
configuration changes.

The production installation links the plugin source from
`/home/ubuntu/AI-Video-Transcriber/openclaw-plugin`, installs the skill as a
real directory under `~/.openclaw/workspace/skills`, and runs the worker and
cleanup timer as user services. Verify them with:

```bash
openclaw plugins inspect video-knowledge-ingest --json
openclaw skills info video-knowledge-ingest
openclaw status --deep
systemctl --user status video-ingestion-worker.service video-ingestion-cleanup.timer
```

OpenClaw 2026.7.1 dispatches `inbound_claim` only for plugin-owned conversation
bindings. The plugin therefore also uses `message_received` to enqueue ordinary
Telegram DMs and `before_agent_reply` to return the acknowledgement before any
model call. It remains the only Telegram polling consumer.

Secrets stay in `.env` or the existing OpenClaw secret env. Set
`YTDLP_COOKIES_FILE` to a mode-0600 Netscape cookies file only when a platform
requires authentication. Production currently points this setting at a
mode-0600 combined cookie jar containing only the required YouTube and Bilibili
domains. Keep platform-specific source jars separately so either platform can
be refreshed without losing the other.

Bilibili short links require a current browser identity on this host. Keep
`YTDLP_USER_AGENT` set to the validated Chrome User-Agent; both short-link
redirects and full `www.bilibili.com/video/BV...` extraction use it.

Current YouTube extraction also uses the `mweb` client, yt-dlp's Node-backed EJS
solver, and the pinned `bgutil-ytdlp-pot-provider` plugin. Its provider runs only
on localhost and is installed with:

```bash
docker run --name bgutil-provider --restart unless-stopped -d --init \
  -p 127.0.0.1:4416:4416 \
  brainicism/bgutil-ytdlp-pot-provider:1.3.1
```

Keep `YTDLP_YOUTUBE_PLAYER_CLIENT=mweb` in `.env`. The health check reports
`youtube_pot_provider=false` when cookies are configured but the local provider
is unavailable.

Jobs waiting at `AWAITING_DESTINATION` live in PostgreSQL. Restarting either
OpenClaw or the worker does not remove them; the Telegram callback reopens the
same job. `PERSISTING` writes are idempotent. On worker startup, any processing
state left by the prior single worker is recovered immediately with a bounded
retry rather than waiting for the normal job timeout.

Telegram users can send `/video_status`, `/video_pause`, `/video_resume`, and
`/video_cancel`. The commands automatically target the active single-video job,
author-list discovery, or author batch across YouTube, Bilibili, and
Xiaohongshu. Replies are generated directly from PostgreSQL, so completion is
not inferred from conversational memory or RAG retrieval. Short Telegram status
questions such as `好了吗`, `完成了吗`, and `录入进度` are intercepted by the
plugin and use the same deterministic query instead of invoking the model.

The worker sends branch-accurate progress notifications. Subtitle-backed URLs
report subtitle retrieval and skip download/Whisper claims; URLs without usable
subtitles report audio download and Whisper transcription. Both paths report
Markdown generation, destination selection, chunking/embedding, SQL/pgvector
registration, and final completion. Retry messages include the attempt count.
Ordinary retryable failures use `JOB_RETRY_DELAY_SECONDS` (60 seconds by
default). Authentication failures managed by the Xiaohongshu or Bilibili
session manager remain paused until the refreshed cookie passes a real probe.

Knowledge-base ownership and metadata rules are documented in
`docs/KNOWLEDGE_BOUNDARIES.md`. Production index roots must not contain smoke
fixtures, failed staging output or duplicate cross-knowledge-base documents.

Health checks cover PostgreSQL/pgvector, FFmpeg, yt-dlp, writable staging/temp,
disk space and effective Whisper settings. Temporary media is deleted
immediately after verified staging and the hourly timer removes failed-job
residue after the configured TTL.

## Author batch schema

I1 adds `migrations/0003_author_batch_ingestion.sql`. Multiplatform author
discovery additionally requires
`migrations/0004_multiplatform_author_batch.sql`. Explicit topic selection for
single works and author batches requires
`migrations/0005_explicit_knowledge_destinations.sql`. Complete author feeds and
batches larger than 50 require
`migrations/0006_unlimited_author_batches.sql`. Apply forward migrations
individually in numeric order; do not recursively execute the
`migrations/rollback/` directory.
The `0005` and `0006` rollbacks refuse to discard incompatible production data.
`migrations/rollback/0004_multiplatform_author_batch.sql` restores the YouTube
and 10-item constraints only when no multiplatform audit rows exist.
`migrations/rollback/0003_author_batch_ingestion.sql` refuses to run after
any production batch exists, because batch audit data must then be preserved by
forward-fix migrations.

All author batch feature flags default to `false`. Installing the schema does
not enable author discovery, batch execution or Telegram author-page controls.

For Xiaohongshu author pagination, export a logged-in browser cookie jar to a
mode-0600 file and set `XIAOHONGSHU_COOKIES_FILE` (it falls back to
`YTDLP_COOKIES_FILE`). Install the full `requirements.txt` so the `xhshow`
request signer is present. Do not paste cookie values into `.env`, logs, URLs,
or Telegram messages.
The service rejects symlinks, non-regular files, files owned by another user,
and cookie files that grant any group/other permissions.

## Read-only author discovery

I3–I5 add the shared YouTube URL classifier, database-backed platform request
gate, and a dedicated flat-metadata discovery worker. Discovery uses
`extract_flat`, a hard playlist limit, `skip_download`, and a metadata
allowlist. It does not fetch subtitles, thumbnails, audio, or video, and it
does not create ingestion jobs or batches.

Keep all author discovery flags disabled until migrations `0003` through `0006` are
installed and the worker unit is linked. Batch execution remains independently
disabled. Useful diagnostics:

```bash
cd /home/ubuntu/AI-Video-Transcriber
.venv/bin/python -m backend.ingestion.cli classify \
  --url https://www.youtube.com/@OpenAI/videos
.venv/bin/python -m backend.ingestion.cli discovery-status \
  --discovery-id 00000000-0000-0000-0000-000000000000 \
  --user-id 1000000001
.venv/bin/python -m backend.ingestion.cli expire-discoveries
journalctl --user -u video-author-discovery-worker.service -f
```

`probe` is an operator action for a paused platform circuit. It reserves the
same database gate as normal discovery, and only one half-open probe can run.
It remains available while the corresponding feature flag is disabled so the
release gate does not require enabling an unvalidated platform. Use
`author-acceptance` for non-persistent pagination, media-resolution, and
temporary-download canaries; see `AUTHOR_BATCH_OPERATIONS.md`.
Authentication and bot-check failures reopen the circuit without an automatic
retry time; rate limiting uses bounded exponential backoff.

Telegram and the operator commands also accept Xiaohongshu `xhslink.com` and
`xhslink.cn` share links. The resolver reads exactly one redirect without
automatically following it, applies the public-address guard to both URLs,
requires a supported Xiaohongshu destination, and returns only the canonical
query-free work or author URL. Share tokens are not persisted.
