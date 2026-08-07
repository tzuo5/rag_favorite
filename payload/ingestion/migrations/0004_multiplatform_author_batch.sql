BEGIN;

ALTER TABLE video_author_discoveries
    DROP CONSTRAINT IF EXISTS video_author_discoveries_platform_check;
ALTER TABLE video_author_discoveries
    ADD CONSTRAINT video_author_discoveries_platform_check
    CHECK (platform IN ('youtube', 'bilibili', 'xiaohongshu'));

ALTER TABLE video_author_discovery_items
    DROP CONSTRAINT IF EXISTS video_author_discovery_items_platform_check;
ALTER TABLE video_author_discovery_items
    ADD CONSTRAINT video_author_discovery_items_platform_check
    CHECK (platform IN ('youtube', 'bilibili', 'xiaohongshu'));

ALTER TABLE video_ingestion_batches
    DROP CONSTRAINT IF EXISTS video_ingestion_batches_platform_check,
    DROP CONSTRAINT IF EXISTS video_ingestion_batches_total_count_check;
ALTER TABLE video_ingestion_batches
    ADD CONSTRAINT video_ingestion_batches_platform_check
        CHECK (platform IN ('youtube', 'bilibili', 'xiaohongshu')),
    ADD CONSTRAINT video_ingestion_batches_total_count_check
        CHECK (total_count BETWEEN 1 AND 50);

ALTER TABLE video_ingestion_batch_items
    DROP CONSTRAINT IF EXISTS video_ingestion_batch_items_platform_check;
ALTER TABLE video_ingestion_batch_items
    ADD CONSTRAINT video_ingestion_batch_items_platform_check
    CHECK (platform IN ('youtube', 'bilibili', 'xiaohongshu'));

COMMIT;
