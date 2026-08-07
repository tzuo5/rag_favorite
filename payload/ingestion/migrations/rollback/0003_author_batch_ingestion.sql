-- Roll back I1 only before any production batch exists.
BEGIN;

DO $$
BEGIN
    IF to_regclass('video_ingestion_batches') IS NOT NULL
       AND EXISTS (SELECT 1 FROM video_ingestion_batches LIMIT 1) THEN
        RAISE EXCEPTION
            '0003 rollback refused: video_ingestion_batches contains audit data';
    END IF;
END $$;

DROP TABLE IF EXISTS video_ingestion_batch_notifications;
DROP TABLE IF EXISTS video_ingestion_batch_items;
DROP TABLE IF EXISTS video_ingestion_batches;
DROP TABLE IF EXISTS video_author_discovery_items;
DROP TABLE IF EXISTS video_author_discoveries;
DROP TABLE IF EXISTS video_platform_request_gates;

ALTER TABLE video_ingestion_jobs
    DROP CONSTRAINT IF EXISTS video_ingestion_jobs_locked_destination_check,
    DROP CONSTRAINT IF EXISTS video_ingestion_jobs_notification_mode_check,
    DROP COLUMN IF EXISTS destination_locked,
    DROP COLUMN IF EXISTS notification_mode;

COMMIT;
