# Knowledge boundaries

## Storage roles

- OpenClaw native memory: concise preferences, commitments, identity facts and
  conversation-oriented memory. Do not store raw documents or transcripts.
- Topic RAG: non-cooking video transcripts are explicitly assigned to one of
  `thought-politics`, `tech`, `finance`, `career`, `social-conduct`,
  `literature-culture`, or `general`.
- Cooking RAG: `cooking` uses its independent indexer for recipes,
  ingredients, techniques, cocktails, menus, shopping lists and preparation
  schedules.

## Write rules

1. Each item has one authoritative knowledge-base destination.
2. A single-work URL waits for the user to choose its destination before
   subtitle extraction, download, transcription or enrichment begins.
3. An author batch chooses one destination at confirmation time; every child
   inherits and locks that destination.
4. Production index roots contain no smoke fixtures, temporary output or failed
   staging files.
5. Video documents carry `content_type`, `domain`, `lifecycle` and
   `retrieval_aliases` frontmatter.
6. Source URLs retain stable content identity but remove ephemeral sharing and
   tracking parameters.
7. Runtime ingestion status comes from `video_ingestion_jobs` via
   `/video_status`; RAG retrieval is evidence lookup, not job monitoring.

`legacy` is read-only and is never offered as an ingestion destination.
Destination migration `0005_explicit_knowledge_destinations.sql` converts
historical `main` document rows from their authoritative Markdown path.
