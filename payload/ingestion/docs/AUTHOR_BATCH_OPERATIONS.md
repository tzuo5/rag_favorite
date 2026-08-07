# Author batch operations

Production feature flags default to disabled. Apply migrations `0003`,
`0004_multiplatform_author_batch.sql`, and
`0005_explicit_knowledge_destinations.sql`, and
`0006_unlimited_author_batches.sql`, and
`0007_work_controls.sql` before
installing the batch workers or OpenClaw plugin update.

Read-only checks:

```bash
python -m backend.ingestion.cli batch-metrics
python -m backend.ingestion.cli batch-audit
python -m backend.ingestion.cli security-audit
python -m backend.ingestion.cli release-observation --hours 24
```

The audit is report-only. It checks cached count drift and orphan child links;
it does not mutate rows. `security-audit` returns count-only evidence for
secret-like values in Xiaohongshu-related database rows and never prints row
contents. `release-observation` returns count-only journal evidence, service
uptime, health, batch metrics, consistency, and persistence safety. Its
`observation_complete` field remains false until all four services have been
continuously active for the requested window.

Emergency stop:

```bash
python -m backend.ingestion.cli pause-all-batches
```

This pauses queued/running parents without deleting completed documents. Keep
`AUTHOR_BATCH_YOUTUBE_EXECUTION_ENABLED=false` to prevent new confirmations.

`AUTHOR_BATCH_BILIBILI_ENABLED` controls both Bilibili discovery and execution.
`AUTHOR_BATCH_XIAOHONGSHU_ENABLED` controls the public-profile fallback and
authenticated signed pagination path. The “全部” selection enumerates the feed
until the platform reports that no page remains; the 5/10/20/50 choices remain
bounded shortcuts. Configure `XIAOHONGSHU_COOKIES_FILE` with a
mode-0600 Netscape or JSON browser cookie export (or reuse
`YTDLP_COOKIES_FILE`) to scan beyond the server-rendered first page. The
pagination signer is provided by the pinned `xhshow` dependency. Cookies,
request signatures, and per-note `xsec_token` values are used only in memory
and are never persisted in discovery rows.

Both full profile URLs and Xiaohongshu `xhslink.com`/`xhslink.cn` share links
are accepted at the Telegram boundary. Short links are expanded through one
validated redirect only; the redirect query is discarded before the canonical
author or work URL can be written. An unavailable or non-Xiaohongshu redirect
fails closed instead of falling through to single-video ingestion.

The cookie loader rejects symlinks, non-regular files, files owned by another
user, and files with any group/other permission bits. A secure file can be
prepared without exposing its contents in shell history:

```bash
install -m 0600 /path/to/browser-export.json /secure/path/xhs-cookies.json
```

After setting `XIAOHONGSHU_COOKIES_FILE`, load the same environment files used
by the workers and verify
`author_batch.xiaohongshu.cookie_file.file_status` is `ready`:

```bash
python -m backend.ingestion.cli capabilities
```

At child execution time the worker re-reads the author listing, caches
the returned per-note tokens in memory for 20 minutes, calls the signed note
detail endpoint, and downloads the selected `xhscdn.com` video stream directly.
This avoids depending on yt-dlp's frequently changing Xiaohongshu HTML
extractor. CDN capability paths are fully redacted from worker logs and are not
included in persisted source metadata.

Keep the Xiaohongshu flag disabled until a real authorized account passes
low-frequency acceptance. Captcha, expired authentication, signature errors,
or risk-control responses open the platform circuit and stop automatic
retries.
Authentication and rate-limit pauses require a successful operator probe before
using the controlled resume path.

Operator probe and canary commands intentionally remain available while the
feature flag is disabled. They consume the normal platform gate but do not
create discovery or batch rows:

```bash
# Stage 1: one-item access probe
python -m backend.ingestion.cli probe \
  --url 'https://www.xiaohongshu.com/user/profile/<user-id>'

# Stage 2: bounded, read-only pagination evidence
python -m backend.ingestion.cli author-acceptance \
  --url 'https://www.xiaohongshu.com/user/profile/<user-id>' \
  --limit 50

# Stage 3: resolve one eligible note without returning its transient URL
python -m backend.ingestion.cli author-acceptance \
  --url 'https://www.xiaohongshu.com/user/profile/<user-id>' \
  --limit 5 --resolve-first-video

# Stage 4: resolve, download, probe, and delete one temporary audio canary
python -m backend.ingestion.cli author-acceptance \
  --url 'https://www.xiaohongshu.com/user/profile/<user-id>' \
  --limit 5 --download-first-video
```

The last command reports only byte count, duration, author match, and temporary
cleanup status. It never returns the signed CDN URL. A five-item end-to-end
Telegram batch still requires a deliberately time-bounded flag change and
human review; do that only after all four stages above pass.

Alert when any platform circuit is open, the oldest pending outbox event is
older than 600 seconds, consistency issues are non-zero, a `PAUSED_AUTH` batch
is older than 15 minutes, or free disk falls below `MIN_DISK_FREE_GB`.
