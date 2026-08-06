BEGIN;

ALTER TABLE video_ingestion_jobs
    DROP CONSTRAINT IF EXISTS video_ingestion_jobs_state_check;
ALTER TABLE video_ingestion_jobs
    ADD CONSTRAINT video_ingestion_jobs_state_check CHECK (state IN (
        'RECEIVED','DOWNLOADING','EXTRACTING_SUBTITLES','TRANSCRIBING',
        'BUILDING_MARKDOWN','ENRICHING_METADATA','AWAITING_DESTINATION',
        'PERSISTING','PAUSED_USER','COMPLETED','FAILED','CANCELLED'
    ));

ALTER TABLE video_author_discoveries
    DROP CONSTRAINT IF EXISTS video_author_discoveries_state_check;
ALTER TABLE video_author_discoveries
    ADD CONSTRAINT video_author_discoveries_state_check CHECK (state IN (
        'QUEUED','DISCOVERING','READY','PAUSED_USER','CANCELLED',
        'FAILED','EXPIRED','CONSUMED'
    ));

ALTER TABLE video_author_discoveries
    DROP CONSTRAINT IF EXISTS video_author_discoveries_finished_check;
ALTER TABLE video_author_discoveries
    ADD CONSTRAINT video_author_discoveries_finished_check CHECK (
        state NOT IN ('FAILED','EXPIRED','CONSUMED','CANCELLED')
        OR finished_at IS NOT NULL
    );

DROP INDEX IF EXISTS video_author_discoveries_active_key;
CREATE UNIQUE INDEX video_author_discoveries_active_key
    ON video_author_discoveries (
        telegram_user_id, platform, canonical_author_url
    )
    WHERE state IN ('QUEUED','DISCOVERING','READY','PAUSED_USER');

COMMIT;
