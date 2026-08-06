# Phase 5.3 Gate 8 — Governed Memory Recall Closure

This additive migration closes the gap between governed memory mutation and
read-only recall without changing content/version semantics from Phase 5.1 or
the four Agent-facing mutation contracts from Phase 5.2.

## Invariants

- Revision content, provenance, version chain, metadata, hashes and actors stay
  immutable.
- Only the four embedding lifecycle columns may transition from `pending` or
  `failed` to `ready`, and only inside the SECURITY DEFINER completion routine.
- The runtime role retains no table privileges. It receives EXECUTE only on
  fixed routines.
- Create/update and embedding completion are committed in one application
  transaction, so a real mutation cannot commit as an unsearchable pending
  revision.
- Search returns only the current revision of `active` memories whose embedding
  is ready for the locked model and 1024 dimensions.
- Each returned content field is bounded to 4000 characters and reports whether
  it was truncated, preventing an oversized memory from flooding Agent context.
- Restore through the runtime role refuses a current revision that is not ready.
- Archived, candidate, rejected, historical, pending and failed revisions never
  appear in governed-memory search results.

## Runtime flow

1. Normalize and validate the mutation request using the existing service.
2. Ask loopback-only Ollama for one document embedding before opening the
   database transaction.
3. Execute the existing create/update routine.
4. Unless the mutation is an idempotent replay, execute the embedding completion
   routine on the same connection and transaction.
5. Commit both audit events together.
6. For recall, embed the query and call the bounded read-only search routine.

No approximate index is introduced at the current row count. The existing exact
cosine scan is deterministic and bounded to at most ten returned rows.
