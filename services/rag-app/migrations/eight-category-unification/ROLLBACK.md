# Eight-category unification rollback

Rollback never changes or deletes Topic or Cooking data.

## Restore targets

Use the verified backup at:

`/home/ubuntu/services/rag-app/migrations/eight-category-unification/.runtime/pre-unification-20260728T103822+0800`

Restore these files from their matching backup copies:

- `~/.openclaw/openclaw.json`
- `~/.openclaw/workspace/AGENTS.md`
- both RAG Skills
- `/home/ubuntu/services/rag-mcp/rag_mcp_server.py`
- `/home/ubuntu/services/rag-app/rag.py`
- video ingestion `destinations.py`

Remove only the new unification source/test files after the original files are
restored:

- `retrieval_contracts.py`
- `retrieval_adapters.py`
- unification-specific tests

Do not restore or modify database tables, Vault files, runtime credentials,
Cooking images, or recipe events.

## Activation

1. Run `sha256sum -c BACKUP-SHA256SUMS.txt` in the backup directory.
2. Restore the listed files with owner/mode preserved.
3. Run `openclaw config validate`.
4. Run `openclaw mcp reload`.
5. Probe both servers with `openclaw mcp probe gordon-rag` and
   `openclaw mcp probe cooking-rag`.
6. Confirm the main Agent once again exposes legacy Cooking search/status and
   uses it for saved cooking retrieval.

A Gateway restart is not part of the default rollback. Use it only if hot
reload and MCP cache disposal fail, and obtain production-restart approval
first.

## Verified rollback evidence

The backup configuration passed `openclaw config validate`. The backed-up
Topic server successfully executed a read-only `tech` search. The unchanged
Cooking server successfully executed `cooking_recipe_search`, reported its
writer configured, and retained identical document/chunk counts before and
after the verification.

```text
ORIGINAL_TOPIC_SEARCH=PASS
ORIGINAL_COOKING_SEARCH=PASS
COOKING_CREATE=CONFIGURED
DATABASE_ROW_COUNTS_UNCHANGED=true
```
