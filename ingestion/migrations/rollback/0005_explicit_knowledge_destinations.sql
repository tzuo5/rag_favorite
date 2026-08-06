BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM video_ingestion_jobs
        WHERE selected_destination NOT IN ('main', 'cooking')
        UNION ALL
        SELECT 1 FROM video_knowledge_documents
        WHERE selected_knowledge_base NOT IN ('main', 'cooking')
        UNION ALL
        SELECT 1 FROM video_ingestion_batches
        WHERE selected_destination NOT IN ('main', 'cooking')
    ) THEN
        RAISE EXCEPTION
            'rollback refused: explicit knowledge destinations contain data';
    END IF;
END $$;

ALTER TABLE video_ingestion_jobs
    DROP CONSTRAINT IF EXISTS video_ingestion_jobs_selected_destination_check;
ALTER TABLE video_ingestion_jobs
    ADD CONSTRAINT video_ingestion_jobs_selected_destination_check
    CHECK (selected_destination IN ('main', 'cooking'));

ALTER TABLE video_knowledge_documents
    DROP CONSTRAINT IF EXISTS video_knowledge_documents_selected_knowledge_base_check;
ALTER TABLE video_knowledge_documents
    ADD CONSTRAINT video_knowledge_documents_selected_knowledge_base_check
    CHECK (selected_knowledge_base IN ('main', 'cooking'));

ALTER TABLE video_ingestion_batches
    DROP CONSTRAINT IF EXISTS video_ingestion_batches_selected_destination_check;
ALTER TABLE video_ingestion_batches
    ADD CONSTRAINT video_ingestion_batches_selected_destination_check
    CHECK (selected_destination IN ('main', 'cooking'));

ALTER TABLE video_author_discoveries
    DROP CONSTRAINT IF EXISTS video_author_discoveries_destination_counts_check,
    DROP COLUMN IF EXISTS existing_destination_counts;

COMMIT;
