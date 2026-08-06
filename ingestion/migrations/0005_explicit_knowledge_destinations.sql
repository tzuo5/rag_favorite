BEGIN;

ALTER TABLE video_ingestion_jobs
    DROP CONSTRAINT IF EXISTS video_ingestion_jobs_selected_destination_check;
ALTER TABLE video_knowledge_documents
    DROP CONSTRAINT IF EXISTS video_knowledge_documents_selected_knowledge_base_check;
ALTER TABLE video_ingestion_batches
    DROP CONSTRAINT IF EXISTS video_ingestion_batches_selected_destination_check;

DO $$
BEGIN
    IF to_regclass(
        format('%I.rag_documents', current_schema())
    ) IS NOT NULL THEN
        EXECUTE format($migration$
            WITH unambiguous_matches AS (
                SELECT
                    video.record_id,
                    min(rag.knowledge_base) AS knowledge_base
                FROM %I.video_knowledge_documents video
                JOIN %I.rag_documents rag
                  ON rag.source_name = regexp_replace(
                      video.markdown_path, '^.*/', ''
                  )
                 AND rag.knowledge_base IN (
                    'thought-politics', 'tech', 'finance', 'career',
                    'social-conduct', 'literature-culture', 'general'
                 )
                WHERE video.selected_knowledge_base = 'main'
                GROUP BY video.record_id
                HAVING count(DISTINCT rag.knowledge_base) = 1
            )
            UPDATE %I.video_knowledge_documents video
            SET selected_knowledge_base = matched.knowledge_base
            FROM unambiguous_matches matched
            WHERE video.record_id = matched.record_id
        $migration$, current_schema(), current_schema(), current_schema());
    END IF;
END $$;

UPDATE video_knowledge_documents
SET selected_knowledge_base = CASE
    WHEN markdown_path LIKE '/home/ubuntu/知识库/Thought and Politics/%'
        THEN 'thought-politics'
    WHEN markdown_path LIKE '/home/ubuntu/知识库/Technology/%'
        THEN 'tech'
    WHEN markdown_path LIKE '/home/ubuntu/知识库/Finance and Investment/%'
        THEN 'finance'
    WHEN markdown_path LIKE '/home/ubuntu/知识库/Career Development/%'
        THEN 'career'
    WHEN markdown_path LIKE '/home/ubuntu/知识库/Chinese Social Relations and Conduct/%'
        THEN 'social-conduct'
    WHEN markdown_path LIKE '/home/ubuntu/知识库/Literature and Culture/%'
        THEN 'literature-culture'
    WHEN markdown_path LIKE '/home/ubuntu/知识库/Cooking/%'
        THEN 'cooking'
    WHEN markdown_path LIKE '/home/ubuntu/知识库/职业发展/%'
        THEN 'career'
    WHEN author ILIKE '%老周横眉%'
      OR title ILIKE '%老周横眉%'
      OR title ILIKE '%老周快评%'
        THEN 'thought-politics'
    WHEN author ILIKE '%Mr Jonathan%'
      OR title ILIKE '%领英%'
        THEN 'career'
    WHEN title ILIKE '%API%'
        THEN 'tech'
    WHEN title ILIKE '%奥德赛%'
        THEN 'literature-culture'
    ELSE 'general'
END
WHERE selected_knowledge_base = 'main';

UPDATE video_ingestion_jobs job
SET selected_destination = document.selected_knowledge_base
FROM video_knowledge_documents document
WHERE document.job_id = job.id
  AND job.selected_destination = 'main'
  AND document.selected_knowledge_base <> 'cooking';

UPDATE video_ingestion_jobs
SET selected_destination = 'general'
WHERE selected_destination = 'main';

UPDATE video_ingestion_batches
SET selected_destination = 'general'
WHERE selected_destination = 'main';

ALTER TABLE video_ingestion_jobs
    ADD CONSTRAINT video_ingestion_jobs_selected_destination_check
    CHECK (selected_destination IN (
        'thought-politics', 'tech', 'finance', 'career',
        'social-conduct', 'literature-culture', 'general', 'cooking'
    ));

ALTER TABLE video_knowledge_documents
    ADD CONSTRAINT video_knowledge_documents_selected_knowledge_base_check
    CHECK (selected_knowledge_base IN (
        'thought-politics', 'tech', 'finance', 'career',
        'social-conduct', 'literature-culture', 'general', 'cooking'
    ));

ALTER TABLE video_ingestion_batches
    ADD CONSTRAINT video_ingestion_batches_selected_destination_check
    CHECK (selected_destination IN (
        'thought-politics', 'tech', 'finance', 'career',
        'social-conduct', 'literature-culture', 'general', 'cooking'
    ));

ALTER TABLE video_author_discoveries
    ADD COLUMN IF NOT EXISTS existing_destination_counts jsonb
        NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE video_author_discoveries
    DROP CONSTRAINT IF EXISTS video_author_discoveries_destination_counts_check;
ALTER TABLE video_author_discoveries
    ADD CONSTRAINT video_author_discoveries_destination_counts_check
    CHECK (jsonb_typeof(existing_destination_counts) = 'object');

COMMIT;
