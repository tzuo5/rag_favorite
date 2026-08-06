# Cooking RAG data boundaries

- Source of truth: `the configured Cooking collection root` Obsidian Vault.
- Searchable in the first release: Markdown files outside `.obsidian`.
- Images remain local references; their pixels are not indexed until a separate
  local-only OCR phase is approved and validated.
- Recipe creation may occur only through the fixed, non-overwriting
  `cooking_recipe_create` tool after an explicit current owner request. The tool
  writes one Markdown file and immediately indexes it.
- Existing recipe update, rename, archive, and delete are not part of the first
  release. Retrieval tools remain read-only.
- The cooking database, roles, MCP server, and tool namespace stay separate from
  `rag-favorite` and governed personal memory.
