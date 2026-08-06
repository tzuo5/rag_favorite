BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM video_author_discoveries
        WHERE platform <> 'youtube'
    ) OR EXISTS (
        SELECT 1 FROM video_author_discovery_items
        WHERE platform <> 'youtube'
    ) OR EXISTS (
        SELECT 1 FROM video_ingestion_batches
        WHERE platform <> 'youtube' OR total_count > 10
    ) OR EXISTS (
        SELECT 1 FROM video_ingestion_batch_items
        WHERE platform <> 'youtube'
    ) THEN
        RAISE EXCEPTION
            '0004 rollback refused: multiplatform or >10 item audit data exists';
    END IF;
END $$;

ALTER TABLE video_author_discoveries
    DROP CONSTRAINT IF EXISTS video_author_discoveries_platform_check;
ALTER TABLE video_author_discoveries
    ADD CONSTRAINT video_author_discoveries_platform_check
    CHECK (platform IN ('youtube'));

ALTER TABLE video_author_discovery_items
    DROP CONSTRAINT IF EXISTS video_author_discovery_items_platform_check;
ALTER TABLE video_author_discovery_items
    ADD CONSTRAINT video_author_discovery_items_platform_check
    CHECK (platform IN ('youtube'));

ALTER TABLE video_ingestion_batches
    DROP CONSTRAINT IF EXISTS video_ingestion_batches_platform_check,
    DROP CONSTRAINT IF EXISTS video_ingestion_batches_total_count_check;
ALTER TABLE video_ingestion_batches
    ADD CONSTRAINT video_ingestion_batches_platform_check
        CHECK (platform IN ('youtube')),
    ADD CONSTRAINT video_ingestion_batches_total_count_check
        CHECK (total_count BETWEEN 1 AND 10);

ALTER TABLE video_ingestion_batch_items
    DROP CONSTRAINT IF EXISTS video_ingestion_batch_items_platform_check;
ALTER TABLE video_ingestion_batch_items
    ADD CONSTRAINT video_ingestion_batch_items_platform_check
    CHECK (platform IN ('youtube'));

COMMIT;
