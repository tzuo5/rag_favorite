-- Consolidate all RAG source paths under /home/ubuntu/知识库.
-- This changes only source metadata; document hashes, chunks, and vectors stay
-- unchanged because the underlying files were moved without modification.

BEGIN;

UPDATE rag_documents
SET source_path = CASE
        WHEN knowledge_base = 'legacy' THEN regexp_replace(
            source_path,
            '^/home/ubuntu/services/rag-app/data',
            '/home/ubuntu/知识库/旧通用库'
        )
        ELSE regexp_replace(
            source_path,
            '^/home/ubuntu/',
            '/home/ubuntu/知识库/'
        )
    END,
    source_relative_path = CASE
        WHEN knowledge_base = 'legacy' THEN ltrim(regexp_replace(
            source_path,
            '^/home/ubuntu/services/rag-app/data',
            ''
        ), '/')
        ELSE source_relative_path
    END,
    indexed_at = now()
WHERE source_path LIKE '/home/ubuntu/%'
  AND source_path NOT LIKE '/home/ubuntu/知识库/%';

DO $$
BEGIN
    IF (
        SELECT count(*)
        FROM rag_documents
        WHERE source_path NOT LIKE '/home/ubuntu/知识库/%'
    ) <> 0 THEN
        RAISE EXCEPTION 'knowledge-root consolidation left paths outside the root';
    END IF;

    IF (SELECT count(*) FROM rag_documents) <> 32 THEN
        RAISE EXCEPTION 'unexpected document count after knowledge-root consolidation';
    END IF;
END
$$;

COMMIT;
