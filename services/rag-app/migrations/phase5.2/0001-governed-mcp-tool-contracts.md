# Phase 5.2: Governed MCP Mutation Tool Contracts

## 1. Status

```text
phase=5.2
artifact=tool-contract-design
implementation_status=NOT_STARTED
registration_status=NOT_REGISTERED
production_status=UNCHANGED
```

This document defines the Agent-facing and internal mutation contracts before
any mutation code, database role, stored procedure, or OpenClaw allowlist
change is implemented.

## 2. Confirmed Baseline

```text
MCP language=Python 3.12
MCP SDK=mcp/FastMCP 1.28.1
MCP transport=stdio subprocess
Existing MCP tools=rag_status,rag_search
Existing mutation tools=none
Existing Agent allowlist=rag_status,rag_search
Current MCP database role=rag_admin
Current MCP role is PostgreSQL superuser=true
Current MCP role owns governed tables=true
```

The existing `rag.py` command surface includes administrative schema and
ingestion operations. Agent tools must never expose arbitrary `rag.py`
subcommands, arbitrary shell execution, or arbitrary SQL.

## 3. Tool Names

Existing tools use the `rag_` prefix. Phase 5.2 will use:

```text
rag_memory_create
rag_memory_update
rag_memory_archive
rag_memory_restore
rag_memory_get
rag_memory_history
rag_memory_events
```

No hard-delete tool will exist.

## 4. Required Schema Delta

`current_version` tracks immutable content revisions. Archive and restore do
not create content revisions, so `current_version` alone cannot detect
lifecycle-only races.

Add to `public.rag_memories`:

```sql
state_version BIGINT NOT NULL DEFAULT 1
CHECK (state_version >= 1)
```

Version behavior:

```text
create:  current_version=1, state_version=1
update:  current_version+1, state_version+1
archive: current_version unchanged, state_version+1
restore: current_version unchanged, state_version+1
```

No production schema change is authorized by this design artifact.

## 5. Server-Owned Identity

The Agent must not provide:

```text
actor
created_by
updated_by
archived_by
```

These are derived from fixed MCP runtime configuration.

Initial actor:

```text
openclaw-agent:main
```

## 6. Input Policy

Agent-writable namespaces:

```text
personal_memory
project_memory
agent_observations
quarantine
```

Not Agent-writable:

```text
knowledge_documents
```

Allowed memory types:

```text
fact
preference
decision
plan
constraint
project_state
procedure
observation
reference
```

Allowed source types:

```text
user_explicit
user_implicit
agent_inference
external_document
tool_output
system_generated
```

Allowed trust levels:

```text
untrusted
low
medium
high
```

Trust policy:

```text
trust_level=high requires source_type=user_explicit
source_type=agent_inference may not use trust_level=high
namespace=agent_observations may not use trust_level=high
namespace=quarantine requires trust_level=untrusted
```

The service derives initial status:

```text
user_explicit + high trust + non-quarantine namespace -> active
all other accepted creates                            -> candidate
```

The Agent cannot directly create `archived` or `rejected` memories.

## 7. Field Limits

```text
content:
  normalized UTF-8 bytes: 1..32768
  trimmed value must not be blank

reason:
  normalized UTF-8 bytes: 1..1024
  trimmed value must not be blank

source_ref:
  optional
  UTF-8 bytes: 1..2048

idempotency_key:
  ASCII
  length: 8..255
  regex: ^[A-Za-z0-9][A-Za-z0-9._:-]{7,254}$

memory_id:
  canonical UUID

expected_version:
  integer >= 1

expected_state_version:
  integer >= 1

metadata:
  JSON object
  serialized UTF-8 size <= 4096 bytes
```

Allowed metadata keys:

```text
tags
project
conversation_ref
source_title
language
```

Unknown metadata keys and nested objects are rejected.

## 8. Content Normalization

Before hashing and persistence:

```text
normalize CRLF and CR to LF
remove UTF-8 BOM
trim leading and trailing whitespace
preserve internal whitespace
```

`content_hash` is lowercase SHA-256 over normalized UTF-8 content.

## 9. Idempotency

Every mutation requires an `idempotency_key`.

The service computes a request fingerprint from:

```text
operation
memory_id
expected_version
expected_state_version
namespace
memory_type
normalized content hash
source_type
source_ref
trust_level
reason
sanitized metadata
```

Plaintext content must not be stored in the fingerprint or event state.

Duplicate behavior:

1. No event with the key:
   - perform the mutation;
   - store the request hash in the audit event.

2. Same key and same request hash:
   - perform no second mutation;
   - return the original result;
   - return `replayed=true`.

3. Same key and different request hash:
   - return `IDEMPOTENCY_CONFLICT`;
   - perform no mutation.

The unique event idempotency constraint remains the final race guard.

## 10. Optimistic Concurrency

Update, archive, and restore require:

```text
expected_version
expected_state_version
```

The repository must lock the memory row using:

```sql
SELECT ... FOR UPDATE
```

The transaction verifies both version tokens before inserting any new
revision or event.

Mismatch returns:

```text
CONCURRENCY_CONFLICT
```

All writes must roll back on conflict.

## 11. Success Envelope

```json
{
  "ok": true,
  "operation": "create|update|archive|restore",
  "memory_id": "uuid",
  "revision_id": "uuid|null",
  "event_id": 123,
  "current_version": 1,
  "state_version": 1,
  "status": "candidate|active|archived",
  "content_hash": "sha256|null",
  "replayed": false,
  "request_id": "uuid"
}
```

Mutation responses do not echo memory content.

## 12. Error Envelope

```json
{
  "ok": false,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "The request is invalid.",
    "retryable": false,
    "field": "content"
  },
  "request_id": "uuid"
}
```

Allowed error codes:

```text
VALIDATION_ERROR
NOT_FOUND
CONCURRENCY_CONFLICT
INVALID_STATE
ALREADY_ARCHIVED
NOT_ARCHIVED
IDEMPOTENCY_CONFLICT
PERMISSION_DENIED
TIMEOUT
DATABASE_UNAVAILABLE
INTERNAL_ERROR
```

Agent-facing errors must not expose SQL, credentials, stack traces, internal
paths, connection details, or memory content.

## 13. rag_memory_create

Input:

```json
{
  "namespace": "project_memory",
  "memory_type": "decision",
  "content": "...",
  "source_type": "user_explicit",
  "source_ref": "...",
  "trust_level": "high",
  "reason": "...",
  "idempotency_key": "...",
  "metadata": {}
}
```

One transaction must:

1. validate and normalize input;
2. generate memory and revision UUIDs;
3. derive initial status;
4. insert the memory with both versions set to 1;
5. insert immutable revision version 1;
6. insert the create event;
7. commit only after deferred constraints pass.

The event state may contain IDs, versions, status, hashes, operation, and
request hash. It must not contain content or embeddings.

## 14. rag_memory_update

Input:

```json
{
  "memory_id": "uuid",
  "expected_version": 1,
  "expected_state_version": 1,
  "content": "...",
  "source_type": "user_explicit",
  "source_ref": "...",
  "trust_level": "high",
  "reason": "...",
  "idempotency_key": "...",
  "metadata": {}
}
```

Allowed source states:

```text
candidate
active
```

Rules:

1. lock the memory row;
2. validate both version tokens;
3. verify the current revision;
4. reject content identical to the current normalized content;
5. insert a new immutable revision;
6. update the current revision pointer;
7. increment both versions;
8. insert an update event;
9. commit atomically.

Archived memories must be restored before content update.

## 15. rag_memory_archive

Input:

```json
{
  "memory_id": "uuid",
  "expected_version": 2,
  "expected_state_version": 2,
  "reason": "...",
  "idempotency_key": "...",
  "source_ref": "..."
}
```

Allowed source states:

```text
candidate
active
```

The transaction updates:

```text
status=archived
archived_at=now
archived_by=server actor
archive_reason=reason
updated_by=server actor
updated_at=now
state_version=state_version+1
```

No revision is created or deleted.

## 16. rag_memory_restore

Input:

```json
{
  "memory_id": "uuid",
  "expected_version": 2,
  "expected_state_version": 3,
  "reason": "...",
  "idempotency_key": "...",
  "source_ref": "..."
}
```

Only archived memories may be restored.

The transaction:

1. locks and validates the memory;
2. verifies both version tokens;
3. changes status to active;
4. clears archive fields;
5. increments state version;
6. inserts a restore event;
7. commits atomically.

No revision is created.

## 17. Read-Back Tools

`rag_memory_get` returns the current memory and current revision.

`rag_memory_history` returns immutable revisions ordered by version
descending.

`rag_memory_events` returns redacted audit events ordered by event ID or
creation time descending.

Archived memories remain readable by ID.

Event output never includes content or embeddings.

## 18. Logging and Timeouts

Permitted log fields:

```text
request_id
tool_name
operation
memory_id
revision_id
event_id
expected_version
expected_state_version
result_version
result_state_version
status
error_code
duration_ms
replayed
```

Forbidden default log fields:

```text
content
old_content
new_content
embedding
database password
connection string
raw SQL
stack trace
complete metadata
```

Timeout policy:

```text
database connect_timeout=5 seconds
database lock_timeout=3 seconds
database statement_timeout=10 seconds
idle_in_transaction_session_timeout=10 seconds
MCP mutation timeout=30 seconds
```

## 19. Python Module Boundary

```text
rag-mcp/
├── rag_mcp_server.py
├── memory_api/
│   ├── __init__.py
│   ├── contracts.py
│   ├── errors.py
│   ├── policy.py
│   ├── service.py
│   ├── repository.py
│   └── database.py
└── tests/
```

Responsibilities:

```text
contracts.py  -> Pydantic input and output schemas
errors.py     -> stable domain errors
policy.py     -> namespace, type, trust and metadata policy
service.py    -> normalization, hashing and orchestration
repository.py -> parameterized database calls and transactions
database.py   -> dedicated runtime connection and timeout policy
```

## 20. Database Privilege Boundary

Planned roles:

```text
rag_memory_api_owner:
  NOLOGIN
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  owns controlled mutation routines

rag_mcp_runtime:
  LOGIN
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOBYPASSRLS
  owns no tables
  has no DROP, ALTER or TRUNCATE
  has no direct mutation privileges
  executes only controlled routines
```

The MCP runtime must stop using `rag_admin`.

The administrative ingestion CLI may retain a separately protected
administrative credential.

## 21. Stored Routine Boundary

Preferred routines:

```text
public.rag_api_memory_create
public.rag_api_memory_update
public.rag_api_memory_archive
public.rag_api_memory_restore
```

Required properties:

```text
SECURITY DEFINER
fixed search_path
non-login non-superuser owner
PUBLIC execute revoked
rag_mcp_runtime execute only
no dynamic SQL
single transaction per tool call
```

The runtime role receives no direct INSERT, UPDATE, DELETE, or TRUNCATE
privileges on governed tables.

## 22. Rollout Gate

Mutation tools must not yet be added to the production allowlist.

Required order:

1. implement and test schema delta in a disposable database;
2. create the least-privilege role and routine boundary;
3. implement repository and service;
4. run transaction and unit tests;
5. register tools in an isolated MCP configuration;
6. expose tools to one test Agent;
7. perform production preflight and backups;
8. enable production configuration;
9. execute governed smoke tests;
10. generate the completion record.

The production filter remains:

```text
rag_status
rag_search
```

## 23. Required Test Matrix

```text
create success
create candidate-status policy
create active-status policy
invalid namespace
knowledge_documents rejection
invalid memory type
trust/source rejection
metadata rejection
content-size rejection
duplicate idempotency replay
idempotency payload conflict
update success
identical-content rejection
stale current-version rejection
stale state-version rejection
revision rollback on conflict
archive success
repeated archive behavior
restore success
restore non-archived rejection
archive/restore lifecycle CAS
cross-memory revision rejection
revision immutability
event immutability
event content-leakage rejection
unauthorized direct DML rejection
unauthorized routine execution rejection
transaction atomicity
timeout rollback
Agent-safe error mapping
read-tool regression
production allowlist unchanged before rollout
```

## 24. Design Gate

```text
PHASE_5_2_STEP_4_TOOL_CONTRACT_DESIGN=PASS
MUTATION_TOOLS_REGISTERED=false
PRODUCTION_CONFIGURATION_CHANGED=false
PRODUCTION_DATABASE_CHANGED=false
```
