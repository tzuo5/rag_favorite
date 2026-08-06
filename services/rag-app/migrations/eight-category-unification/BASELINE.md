# Eight-category unification baseline

Captured at: `2026-07-28T10:38:22+08:00`

## Configuration hashes

| File | SHA-256 |
|---|---|
| `~/.openclaw/openclaw.json` | `843a033da7b92eb03a35af3d20c948e7519b3e37dc6c10da873f90340fbac4c4` |
| `~/.openclaw/workspace/AGENTS.md` | `a5cfb232f63cce67d158eea0e75c1adffbe66c650ddeb27d0f3922a9b258b9b6` |
| `gordon-private-rag/SKILL.md` | `edfd2ba7c34e6cf7786fc1ff633ee3269be0e49e81343065a70dc469d85bb445` |
| `gordon-cooking-rag/SKILL.md` | `8a8b373a075823e174e1c47eea85d681bf0fc2b00640cfffc819fcf8552ff3b6` |
| `rag_mcp_server.py` | `8171258f722ffbea39a6201d2d4bfaf0bef2a8b216a85aa4623afd8d4af7a3cf` |
| Cooking `mcp_server.py` | `3abf87e6df60c4da0ca65979ea022b82867e65509ed406fdf0b61fc8801fa4c5` |

## MCP tools

- `gordon-rag`: `rag_memory_create`, `rag_memory_update`,
  `rag_memory_archive`, `rag_memory_restore`, `rag_status`, `rag_search`
- `cooking-rag`: `cooking_recipe_search`, `cooking_recipe_create`,
  `cooking_recipe_get`, `cooking_rag_status`

## Indexed data

| Knowledge base | Documents | Chunks |
|---|---:|---:|
| `thought-politics` | 54 | 1,515 |
| `tech` | 3 | 14 |
| `finance` | 1 | 1 |
| `career` | 101 | 428 |
| `social-conduct` | 225 | 986 |
| `literature-culture` | 1 | 16 |
| `general` | 0 | 0 |
| `cooking` | 647 | 4,618 |
| **Eight-category total** | **1,032** | **7,578** |

The Topic database also contained the read-only `legacy` migration collection:
16 documents and 178 chunks. It is intentionally excluded from the eight
searchable categories.

## Backup

Backup directory:

`/home/ubuntu/services/rag-app/migrations/eight-category-unification/.runtime/pre-unification-20260728T103822+0800`

The backup contains 45 source/config/test files plus
`BACKUP-SHA256SUMS.txt`. `sha256sum -c` passed.

```text
BASELINE_CAPTURED=true
CONFIG_BACKUP_VERIFIED=true
DATABASE_ROW_COUNTS_CAPTURED=true
```
