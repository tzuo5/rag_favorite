# Unified knowledge library and legacy migration

The owner's profile now supports one knowledge root, whole-library summary retrieval,
and versioned original reading. Topic/type labels are display and provenance metadata;
they do not select paths, restrict candidates, affect ranking, or enter embedding input.
Older topic arguments remain accepted as aliases of the complete library.

The local rollout is tracked in `.runtime/migrations/unified-20261010/`:

- `database-before.dump`: private PostgreSQL backup, including the original tables.
- `before/`: saved original configuration, queue control and selected source files.
- `manifest.json`: original hashes, attachment references, document identities, source
  URLs and corresponding reprocessing jobs. Signed source URLs are private local data.
- `report.json`: current stage, actual completed counts and failures.

The migration service is `rag-favorite-knowledge-migration.service`. It pauses new
video/summary claims, waits for in-flight jobs, builds a shadow summary generation,
validates coverage, switches the configuration, creates fresh reprocessing tasks,
reloads the workers/Web console, and restores the owner's prior queue pause setting.
OCR, source copying, summary embeddings and task submission are checkpointed so the
same migration can resume without repeated OCR or repeated queue submissions.

## Source migration

Markdown deduplication includes both original content and referenced image hashes.
Source paths for every duplicate are retained. Obsidian wiki links (including
extensionless document links and image filenames containing `#`) resolve before
copying; unresolved links stop the migration. Attachments use content hashes and
are checked after copying. Original legacy Markdown is also kept next to the
materialized knowledge draft. The backup tree remains intact.

All old images receive local OCR, cached by image hash. OCR quotes preserve source
identity and confidence; confidence below 0.85 receives an explicit uncertainty
marker. Unreferenced images are separate source documents. Images with no detected
text stay preserved and are reported as unsearchable through text retrieval.

Legacy summaries initially use a deterministic extractive method: select source sentences using paragraph coverage, title relevance and explicit
number/uncertainty cues, capped at approximately 3,400 characters including
paragraph references. Unusually long sentences are marked as incomplete excerpts. This is
not a new model interpretation of the source. Existing published media summaries
retain their content; only their embedding input drops topic/source annotations.
Unpublished current media drafts are retained without being promoted as completed.

## Reprocessing original sources

Each supported original source (Xiaohongshu, YouTube, Bilibili) receives one fresh
job per migration, even if an older job is complete. New task IDs keep existing
job state and checkpoints intact. Re-running migration returns the same new task
IDs. Tasks use the existing normal queue priority and are not reported as processed
merely because they were submitted.

Xiaohongshu notes are processed according to their actual media type. YouTube and
Bilibili downloads feed the existing staged-asset ASR/visual/summary pipeline using
yt-dlp. Download limits and private optional cookie files are preserved. A source
that cannot be fetched is recorded as a failed task; its migrated old knowledge
remains available. A fresh summary supersedes old documents in search only when
its source task is complete. Old documents remain addressable for provenance.

## Data and interfaces

Migration `0007_unified_library.sql` adds generic document, summary, chunk, task,
generation and reprocessing-ledger tables. No old migration SQL or old tables are
rewritten. Media documents retain UUIDs and the `video:` read alias. Generic sources
use `knowledge:` IDs. Summary/draft snapshots are versioned within a generation,
and readers verify hashes and pagination versions.

`rag_search(query, limit)` searches the whole library, returns at most five distinct
summary documents, and orders them using the existing cosine similarity. Type and
source annotations are excluded from `search_text`. `document_read` returns the
selected complete summary and paginated original. Agents should judge relevance
from content, preserve source uncertainty, and treat source text as evidence.
Existing Codex MCP sessions must reconnect to load the changed server profile.

## Exact video-title deduplication

The follow-up migration `0008_video_title_dedup.sql` adds a title identity and a
reversible task-alias ledger. Matching is exact after Unicode width and whitespace
normalization. Only legacy filenames lose their known `--<eight hex digits>`
suffix. Episode numbers and punctuation remain significant. Empty titles, source
IDs and subtitle time-range placeholders are not identities. Their actual title
is recovered from source metadata before expensive download/transcription.

Source-backed knowledge with the same title occupies one result slot; the window
is applied before the top-five limit. Each result/read response lists other
same-title documents in `metadata.same_title_documents`, whose originals remain
readable with their original IDs and hash checks. Manual documents without video
sources are not merged just because their headings match. Topic tags remain
display metadata and are not used by this rule.

New same-title submissions reuse an existing complete or active job and retain
the additional source. Explicit legacy reprocessing still creates one fresh
task per title even when older versions are complete. Duplicate queued/blocked
tasks have state `duplicate` and stage `title_duplicate`, displayed as “已合并”.
Their prior payload/state/stage and canonical task are retained in
`rag_title_job_aliases`; completed originals and in-flight work are preserved.
A successful fresh task supersedes all old same-title document IDs in search,
without deleting their original files.

The local 2026-10-10 audit/application is stored in
`.runtime/migrations/title-dedup-20261010/`: `before.json` is the private original
snapshot and `report.json` records the applied merges. It merged 90 queued tasks
and two blocked tasks. There were 38 duplicate document-title groups (45 excess
records), and 47 task titles need source metadata before a decision. A second dry
audit found zero further task merges and zero duplicate pending title groups.
204 related tests passed, including isolated live PostgreSQL and GUI tests.
`mcp-acceptance.json` records a new real stdio MCP connection with three successful
top-five searches and paginated original hash checks. This is logical deduplication;
it does not delete backups or imply original-video reprocessing is complete.
Both core and video MCP entrypoints passed real transport checks. After the active
video completed, the worker reloaded with `title_dedup_version=1`, all three runtime
services were active, and the original unpaused queue setting was restored;
`reload-receipt.json` and `final-runtime.json` contain the actual receipts.

```bash
.venv/bin/python -m rag_favorite.title_dedup            # read-only audit
.venv/bin/python -m rag_favorite.title_dedup --apply    # save snapshot, merge tasks
```

Pause new claims before an operational backfill, reload workers after the current
job finishes, and restore the owner's previous pause state. Keep report/snapshot
files private because they contain source provenance and access URLs.

For the unified profile, `rag-favorite index create/build/activate/list` operates
on summary generations. Building an active generation is rejected. Activating an
incomplete or changed-source generation is rejected; `--allow-stale` permits an
explicit snapshot rollback. Direct Markdown ingestion registers a generic document
and creates its source-verified summary rather than a second body-search index.

## Operational commands

Use the repository's `.venv/bin/python` (some installed executable shebangs still
point to the old checkout path). Supply the actual profile paths:

```bash
export RAG_FAVORITE_CONFIG="$PWD/.runtime/phase1/config.toml"
export RAG_VIDEO_CONFIG="$PWD/.runtime/videorag/video.toml"
.venv/bin/python -m rag_favorite.knowledge_migration '.backup-original/知识库'
.venv/bin/python -m rag_favorite.knowledge_migration '.backup-original/知识库' --apply
.venv/bin/python -m rag_favorite.video_cli summary-status
.venv/bin/python -m rag_favorite.video_cli search '如何通过领英获得内推'
journalctl --user -u rag-favorite-knowledge-migration.service -n 10 --no-pager
```

On failure before activation, the original retrieval configuration remains in use.
On a cutover failure, the saved original configuration is restored. To roll back a
completed cutover, pause claims, wait for in-flight tasks, restore the saved config,
reload the video/summary workers and Web console, and restore the previous pause
state. Fresh migration tasks must stay paused during rollback because their unified
storage alias is not configured in the old profile. The old tables and old source
files remain available; a PostgreSQL restore is unnecessary for this additive
rollback. Preserve the backup until independent acceptance is finished.

## Verification

177 related unit/regression/live-database tests passed during implementation,
including GUI IPC responsiveness and Web authorization. New tests cover attachment-
aware deduplication, wiki links, source identities, forbidden source hosts, topic-
independent embedding input, OCR uncertainty, shadow generation activation,
versioned original reading, failed publication preserving old results, interrupted
publication recovery without reembedding, and fresh exactly-once reprocessing.
Database tests use isolated library IDs and clean up.
The same 177 checks passed again after the active profile switched; legacy tests
pin their feature mode and temporary storage independently of the owner's profile.

The migration automatically checks real stdio MCP search, legacy topic aliases,
paginated original reading and source hashes before submitting reprocessing jobs.
The live migration and real video jobs have separate acceptance: consult the report.
Report copied,
summary-published, queued, failed and successfully reprocessed counts separately.

## Completed local cutover on 2026-10-10

Generation `unified-20261010` was activated with 2,084 searchable documents:
1,216 legacy Markdown bundles, 47 standalone OCR image documents, and 821 existing
published media summaries. All 1,217 unique legacy Markdown bundles and 893 images
were preserved. The image-only recipe `千层土豆配美乃滋` has no detected text and
remains preserved but unsearchable through text retrieval. Ten unfinished current
drafts were preserved without promotion.

The source ledger contains exactly 1,164 distinct new job IDs: 1,105 Xiaohongshu,
56 YouTube and three Bilibili sources. They were queued for full reprocessing;
queue submission does not mean those sources have finished. Five preexisting
summary failures were also scheduled for retry, so the searchable count can grow
after this snapshot.

Both fresh stdio MCP entrypoints passed real search and complete original-hash
checks for cocktail, API and LinkedIn referral queries. The main entrypoint also
works with just `RAG_FAVORITE_CONFIG`, using its configured video profile.
See `mcp-acceptance.json` and `primary-mcp-acceptance.json` in the migration run.
The two MCP connections already open in this Codex session still returned the old
821-document profile and need reconnection to load the updated code/configuration.

Video/summary workers and the Web console were reloaded and are active. The
original global queue setting (`paused=false`) was restored. The migration
service exited successfully. Original backups and old tables remain intact.
