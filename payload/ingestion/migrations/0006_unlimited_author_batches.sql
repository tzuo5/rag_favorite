BEGIN;

ALTER TABLE video_author_discoveries
    DROP CONSTRAINT IF EXISTS video_author_discoveries_scan_limit_check;
ALTER TABLE video_author_discoveries
    ADD CONSTRAINT video_author_discoveries_scan_limit_check
    CHECK (scan_limit >= 0);

ALTER TABLE video_ingestion_batches
    DROP CONSTRAINT IF EXISTS video_ingestion_batches_total_count_check;
ALTER TABLE video_ingestion_batches
    ADD CONSTRAINT video_ingestion_batches_total_count_check
    CHECK (total_count >= 1);

COMMIT;
