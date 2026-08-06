# Eight-category unification implementation report

Date: `2026-07-28`

## Completed

- Captured hashes, MCP tool lists, eight-category counts, and a verified backup.
- Added the eight-value contract, safe result/status structures, Adapter
  protocol, Topic Adapter, and Cooking Adapter.
- Added `cooking` to unified `rag_search` and `rag_status`.
- Added explicit cross-library Cooking support with per-category rank fusion;
  raw Topic cosine and Cooking hybrid scores are not compared as if they share
  a scale.
- Removed absolute host paths from Topic source output and redacted host paths
  inside returned titles, sections, and excerpts.
- Preserved the Cooking Vault, tables, hybrid retriever, create/get, and media
  delivery.
- Added the video worker `KnowledgeIndexer` protocol,
  `TopicIndexerAdapter`, `CookingIndexerAdapter`, and
  `UnifiedIndexingService`.
- Updated AGENTS, both RAG Skills, and the main Agent allowlist. Legacy Cooking
  search/status remain on the MCP server for rollback but are absent from the
  default main Agent route.
- Validated config, hot reload, MCP cache reload, Gateway health, protocol
  schemas, production status, read-only search, full recipe get, media
  directives, prompt-injection isolation, and rollback.

## Gates

```text
BASELINE_CAPTURED=true
CONFIG_BACKUP_VERIFIED=true
DATABASE_ROW_COUNTS_CAPTURED=true
EIGHT_CATEGORY_CONTRACT=PASS
TOPIC_ADAPTER_TESTS=PASS
COOKING_ADAPTER_TESTS=PASS
COOKING_VIA_UNIFIED_SEARCH=PASS
COOKING_VIA_UNIFIED_STATUS=PASS
ORIGINAL_COOKING_MCP_UNCHANGED=true
UNIFIED_RETRIEVAL_CANARY=PASS
NO_UNEXPECTED_WRITES=true
NO_ABSOLUTE_PATH_LEAK=true
COOKING_SPECIAL_PRIORITY_REMOVED=true
COOKING_IS_EIGHTH_CATEGORY=true
RECIPE_CREATE_STILL_EXPLICIT=true
CONFIG_VALID=true
ORIGINAL_TOPIC_SEARCH=PASS
ORIGINAL_COOKING_SEARCH=PASS
COOKING_CREATE=CONFIGURED
DATABASE_ROW_COUNTS_UNCHANGED=true
```

## Production evidence

- Unified status: 647 Cooking documents and 4,618 Cooking chunks.
- Unified Cooking search: reliable `红烧肉` results with relative paths.
- Unified Topic search: `tech` result from
  `2. Infrastructure and Servers/server-plan.md`.
- Explicit `cooking + literature-culture` search returned both categories for
  the query `食物`.
- No-match Cooking query returned `no_reliable_match=true`.
- Full recipe get used the current search ID, returned a relative source, and
  produced one valid `MEDIA:` directive with no missing or failed image.
- Database counts remained 1,032 documents and 7,578 chunks before and after
  retrieval canaries.
- A non-delivered main-Agent diagnostic used only
  `gordon-rag__rag_search` for Cooking; legacy Cooking search/status were not
  present in its tool list.

## Pending external/time-bound gates

- Owner Telegram DM canary is not sent automatically because it is an external
  message/action requiring explicit approval.
- The 14-day observation period cannot be completed on deployment day.
- Legacy Cooking search/status retirement remains intentionally pending.

Until those gates pass, the implementation is deployed and rollback-ready, but
the old interfaces must not be deleted.
