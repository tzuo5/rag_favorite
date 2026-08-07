BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM video_author_discoveries
        WHERE scan_limit = 0 OR discovered_count > 50
    ) OR EXISTS (
        SELECT 1 FROM video_ingestion_batches
        WHERE total_count > 50
    ) THEN
        RAISE EXCEPTION
            'rollback refused: unlimited author discoveries or batches exist';
    END IF;
END $$;

ALTER TABLE video_author_discoveries
    DROP CONSTRAINT IF EXISTS video_author_discoveries_scan_limit_check;
ALTER TABLE video_author_discoveries
    ADD CONSTRAINT video_author_discoveries_scan_limit_check
    CHECK (scan_limit > 0);

ALTER TABLE video_ingestion_batches
    DROP CONSTRAINT IF EXISTS video_ingestion_batches_total_count_check;
ALTER TABLE video_ingestion_batches
    ADD CONSTRAINT video_ingestion_batches_total_count_check
    CHECK (total_count BETWEEN 1 AND 50);

COMMIT;
