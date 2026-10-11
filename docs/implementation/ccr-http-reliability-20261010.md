# CCR HTTP reliability change, 2026-10-10

## Change and compatibility

The retained CCR sample contained 17 HTTP successes out of 43 requests. Most
failures were 507 and 503, while the effective routing fallback was disabled.
This change enables same-model routing retry with two extra attempts and keeps
GPT-6 Luna medium, the 600-second timeout and eight-frame batches.

`CCRProvider` makes one client request. HTTP errors are handled separately from
network and stream failures. Its existing business error codes remain compatible;
`ProviderUnavailable.diagnostics` adds optional, credential-free details.

The existing provider/summary JSONL ledgers now include request timestamps, client
correlation ID, HTTP status, serialized request size, returned CCR request/retry
headers, failure category and code, bounded error summary, stream completion,
JSON validity, returned model/effort, and `call_kind`. Missing fields in historical
rows remain supported. Files are enforced as mode 0600. Request bodies and images
are not logged. Known request text and credentials are removed from error summaries.

Video and summary content regeneration is marked `content_repair`. Its existing
policy remains in place; it is distinct from gateway transport retries. An HTTP
200 alone is not counted as a usable extraction: stream completion and valid JSON
are recorded separately.

## Verification

- Related Python regressions: 105 passed, 9 skipped. Skips require a separate
  PostgreSQL integration-test database and are not live database acceptance.
- Ruff checks and formatting pass for the changed files.
- Installed CCR 3.1.1 was run with private temporary configuration, independent
  ports and a controlled local upstream. Eight scenarios passed: 503/507 recovery,
  exhausted 503 retry, non-retryable 400, bounded 401 auth refresh, Retry-After on
  429, socket-reset recovery and exhausted socket resets.
- CCR also has an internal socket-reset retry (about 150 ms) and a separate
  once-only 401 auth refresh. Routing retries do not remove those existing
  mechanisms. Persistent socket resets produced six upstream calls in the test;
  persistent 503 produced three. Returned fallback headers describe routing
  attempts, and must not be treated as an exact count of every internal attempt.
- The counter limit was also tested: after admitting one simulated upstream
  request, the next client request was rejected without a second upstream dispatch.
- A production-gateway smoke request after deployment returned HTTP 200, a
  completed stream, valid JSON, GPT-6 Luna and medium reasoning, in 2.06 seconds.
  Its diagnostics are saved separately as `production-smoke-acceptance.json`.

## Real image comparison

`scripts/ccr_reliability.py` runs isolated CCR instances and an upstream forwarding
counter. It copies retained frames without resizing or recompression, preserves
source checkpoint timestamps and transcript, rotates arm order, validates every
frame index, and writes private outputs without publishing them or entering the
production queue. The counter persists an admission before dispatching an upstream
request; its hard limit includes retries.

Four complete paired groups used 16 audited upstream requests; one extra retry8
group used one more request. All 13 completed arms had complete streams, valid
JSON and complete frame coverage. Their returned model/effort were GPT-6 Luna and
medium. Paired whole-group medians were:

| Strategy | Successful groups | Median seconds |
| --- | ---: | ---: |
| Retry off, 8 frames | 4/4 | 8.10 |
| Retry on, 8 frames | 4/4 | 8.99 |
| Retry on, two 4-frame calls | 4/4 | 13.01 |

The four-frame strategy was about 45% slower than retry8 and did not reduce failed
groups. Therefore production stays at eight frames. Contact sheets and returned
captions were reviewed; some subtitle wording and index differences remain, so
this is not a claim of perfect OCR or vision accuracy.

No HTTP failure occurred in these paired samples, so the real trial does not
prove a higher success rate or long-term stability. The bounded retry behavior is
verified with the controlled upstream, including the observed failure classes.

The first trial exposed an OAuth plugin that bypassed the counter and was stopped.
Seven client calls from that run are excluded from comparison; their exact
upstream count cannot be audited. A second counted run was interrupted to reduce
the remaining allowance. Conservative reservations were made using observed
retry maxima, leaving a final counter limit of 28; that final run consumed 17.
The accounting record explicitly distinguishes reservations from measured calls.
Do not report an exact aggregate upstream count for all preliminary runs.

Artifacts: `.runtime/ccr-reliability-20261010-final/{report.json,results.json,
upstream-attempts.json,calls.jsonl,fixtures.json}`. Preliminary runs are retained in
the adjacent `ccr-reliability-20261010*` directories and are not mixed into paired
results. Fixtures and results contain private source material and stay local.

## Rollout and recovery

New queue claims are paused through the shared queue gate; the current job is
allowed to finish before workers are reloaded. CCR configuration is backed up by
SQLite's backup API and a private JSON snapshot before management-RPC updates.
Profiles, provider selection and reasoning settings are preserved.

Use `scripts/ccr_reliability.py deploy --output
.runtime/ccr-reliability-20261010-final` for the guarded deployment. After rollout,
inspect the private `deployment.json` for the actual gateway and
worker checks. Successful configuration persistence alone is not live acceptance.
The prior queue pause flag is restored after workers start. No failed jobs are
bulk-requeued and no model or encoder service is restarted.

Rollout completed: the drained video is `complete`, both worker services have new
active PIDs, the gateway reports `running`, the effective retry count is 2 and the
queue pause flag is restored to false. The next queued material resumed ASR. Its
first CCR ledger entry had not completed in the 60-second observation window;
production adapter diagnostics are nevertheless verified by the separate smoke
request. This does not establish long-term production reliability.

Rollback files are under the final artifact directory's `rollback/`. The adapter
snapshot is recovered from the pre-edit session read; the visual and summary
snapshots remove this change's content-repair annotation and may retain harmless
formatting changes. Pause new claims and wait for active calls before restoring
these module snapshots. Restore `ccr-config-before.json` through `saveConfig` with
`applyProfile:false`, then reload the workers and restore the prior pause flag.
Review intervening user edits before restoring a whole file. The SQLite backup's
location is in `rollback/backup-location.json`; do not overwrite a live SQLite
database directly.
