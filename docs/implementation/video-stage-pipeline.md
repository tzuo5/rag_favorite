# Video stage pipeline

The default loaded video profile uses four durable PostgreSQL stage queues:
download → prepare → LLM → publish. Each consumer handles one task at a time;
LLM has `ccr_concurrency` consumers, while the other stages retain one each.
Xiaohongshu media, including image and Live Photo bundles, now downloads in the
download stage through the managed session. OCR remains in prepare. See
[80-item node rotation](xhs-proxy-80-and-desktop-web-20261011.md).

Different consumers overlap across tasks. CPU ASR and GPU ImageBind have separate
locks. When ASR is configured to use CUDA, it also holds the GPU slot and temporarily
offloads ImageBind so the two models fit in local memory; ASR and ImageBind then run
in turn while LLM processing can overlap. Numbered database advisory slots bound
CCR requests across extraction and historical summary backfill for the library;
another slot serializes summary embedding. An omitted `ccr_concurrency` retains
the serial default of one. The active local profile explicitly requests six:

```toml
pipeline_enabled = true
pipeline_prefetch = 2
ccr_concurrency = 6
pipeline_temp_bytes = 10737418240
```

The coordinator admits at most `pipeline_prefetch + ccr_concurrency + 2` active
tasks: ten with the profile above, or five with the serial default. Preparation
stops admitting work when two tasks are queued for LLM. A retry retains its current
task assets; failed tasks are excluded from prefetch capacity but retained bytes
still count against the temporary storage budget. The budget covers task-owned
source copies and work files. It is checked before operations and after preparation;
an operation that crosses the budget is blocked with its output retained. It is
not a filesystem quota. The existing failed-media TTL still runs in continuous mode.

Concurrency is a maximum, not a requirement to keep six calls in flight. If fewer
tasks are prepared, fewer LLM consumers can work. Each task keeps its own provider,
lease and checkpoint files; the six request slots are shared with summary backfill.
Slot zero keeps the original advisory-lock identity. Runtime changes require a
paused queue, drained operations and a worker reload; the stage scheduler and
request limiter must load the same setting.

Apply migration `0009_video_pipeline.sql` while the old worker is stopped. The
coordinator keeps the original library lock, so an old worker cannot run beside it.
Existing ASR, frame-batch, segment and summary checkpoints remain readable.
Stage handoff and downstream enqueue commit in one transaction. Fifteen-second
heartbeats maintain 120-second leases; session locks and attempt tokens prevent
expired owners from committing. A worker restart recovers expired owners only.

Summary generation validates source citations and saves a candidate. Publication
requires that candidate to match the current source/context; it performs embedding
and database publication without LLM calls. Cleanup remains after verified publication.

## Operation

The installed video-worker service launches the stage coordinator. A paused
continuous coordinator waits for the existing GUI Start/Resume action. Global and
per-task pauses stop new claims and new model operations; an in-flight operation may
finish and save its checkpoint. The GUI shows current phase ownership, queue counts,
retry reasons, retained per-call metrics and searchable completed tasks.

For a bounded acceptance run, with the old video/summary workers stopped, use the
configured environment and `python -m rag_favorite.video_worker --max-jobs 5`.
The queue must first be explicitly resumed. The batch selects a fixed list, prefers
already-admitted work, drains at most those five IDs and automatically pauses.
`pipeline-batch.json` stores its IDs; `pipeline-acceptance.json` stores final states.
The continuous command has no task limit. `--once` selects a single bounded task.

Transient network errors and HTTP 502/503/504 retry at most twice, after 5/30 seconds.
HTTP 507 and authentication/input errors block their task. A transport-truncated
stream is retryable; completed but invalid content remains a separate repair gate.
Three consecutive transport, upstream-buffer (507), or authentication failures
trigger a 60-second cooldown; the next ordinary request probes recovery.
Valid completed JSON resets the streak. Semantic
source validation remains a separate acceptance boundary. Manual retry retains
successful checkpoints; preparation integrity failures rewind the preparation phase.

CCR HTTP success, complete JSON output and final searchable completion are separate
metrics. The GUI's call window is the last 24 hours of retained terminal records
(at most 256 finished activity records); it is not an account-wide lifetime rate.
The local CCR usage database observed before this implementation held 43 calls,
17 HTTP successes, 10 HTTP 507s and 9 HTTP 503s. Captured upstream response text
identified retry-buffer overflow and connection termination before response headers.
Those diagnostics do not establish a local disk issue or a proven payload threshold.
The first acceptance batch also encountered an HTTP 200 whose upstream stream
terminated during the response body (`UND_ERR_SOCKET`), before a complete output
reached the client. A successful HTTP status alone cannot establish successful
generation. The client subsequently timed out and the scheduler released the LLM
stage for another material while retaining the failed material's extraction
checkpoints for its bounded retry. The existing 600-second network-read timeout
is preserved. The later runtime repair closes the gateway's downstream connection
promptly on upstream body errors; see the [runtime repairs](runtime-repairs-20261010.md).

## Verification

Offline phase tests exercise preparation without CCR, LLM reuse without ASR or
ordinary frame decoding, artifact/vector checks, draft recovery and publication
without generation. Isolated PostgreSQL tests cover ownership, leases, atomic
handoff, retry limits, cooldown, prefetch/storage admission and terminal recovery.
The final full suite passed with isolated temporary application directories and
local socket access: 406 passed, 38 skipped. The isolated PostgreSQL stage tests
and queue-monitor database tests were enabled; other environment-dependent tests
remain opt-in.

First live acceptance is limited to five existing tasks, then paused. Its sample
does not establish a fixed speed multiplier. Use stage intervals, per-task usage
ledgers and successful searchable completions to assess overlap and throughput.

### Completed acceptance, 2026-10-10 (America/Chicago)

[Sanitized acceptance report](video-stage-pipeline-acceptance-20261010.json): five
selected tasks completed and are searchable in the active index. All five cleaned
their owned source/work files. Planned and analysed frames both total 1,386.
The batch elapsed 3,437.026 seconds, including one stalled upstream response.
Preparation overlapped CCR client calls for 641.013 seconds; publication overlapped
for 9.547 seconds. Downloading finished before the first CCR call in this batch.
These are observed overlaps, not a controlled before/after speedup measurement.

The job ledgers contain 199 sent requests, all HTTP 200, with 197 complete JSON
outputs. One malformed JSON response was regenerated under the existing content
repair policy; one response timed out after the upstream disconnected and succeeded
on the stage retry. Client call intervals never overlapped. The configured model
remained `Codex API/gpt-6.1-sol`, routed to `gpt-6-luna` with medium reasoning.

At completion the durable queue gate was paused, no stage leases were active, and
temporary pipeline-owned bytes were zero. Video and summary worker services were
inactive; the local encoder remained active. Reopen an already-running desktop
console to load the updated UI, then use Start when ready to process the backlog.
