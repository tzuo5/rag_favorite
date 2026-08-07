BEGIN;

CREATE TABLE IF NOT EXISTS video_ingestion_jobs (
    id uuid PRIMARY KEY,
    telegram_user_id text NOT NULL,
    telegram_chat_id text NOT NULL,
    telegram_message_id text NOT NULL,
    input_kind text NOT NULL CHECK (input_kind IN ('url','media')),
    input_value text NOT NULL,
    media_type text,
    caption text,
    forward_origin jsonb NOT NULL DEFAULT '{}'::jsonb,
    state text NOT NULL CHECK (state IN (
        'RECEIVED','DOWNLOADING','EXTRACTING_SUBTITLES','TRANSCRIBING',
        'BUILDING_MARKDOWN','ENRICHING_METADATA','AWAITING_DESTINATION',
        'PERSISTING','COMPLETED','FAILED','CANCELLED'
    )),
    retry_count integer NOT NULL DEFAULT 0 CHECK (retry_count BETWEEN 0 AND 10),
    next_attempt_at timestamptz,
    processing_started_at timestamptz,
    processing_finished_at timestamptz,
    source_platform text,
    source_id text,
    canonical_url text,
    title text,
    author text,
    language text,
    duration_seconds double precision,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    transcript_checksum text,
    media_sha256 text,
    staging_path text,
    staging_checksum text,
    selected_destination text CHECK (selected_destination IN ('main','cooking')),
    document_id uuid,
    last_error_code text,
    last_error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS video_jobs_runnable_idx
    ON video_ingestion_jobs (state, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS video_jobs_canonical_url_idx
    ON video_ingestion_jobs (canonical_url) WHERE canonical_url IS NOT NULL;
CREATE INDEX IF NOT EXISTS video_jobs_source_idx
    ON video_ingestion_jobs (source_platform, source_id) WHERE source_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS video_jobs_media_sha_idx
    ON video_ingestion_jobs (media_sha256) WHERE media_sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS video_jobs_transcript_sha_idx
    ON video_ingestion_jobs (transcript_checksum) WHERE transcript_checksum IS NOT NULL;

CREATE TABLE IF NOT EXISTS video_knowledge_documents (
    id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES video_ingestion_jobs(id),
    title text NOT NULL,
    original_title text,
    source_platform text NOT NULL,
    source_url text,
    source_id text,
    author text,
    published_at timestamptz,
    captured_at timestamptz NOT NULL,
    language text,
    tags text[] NOT NULL DEFAULT '{}',
    markdown_path text NOT NULL,
    checksum text NOT NULL,
    selected_knowledge_base text NOT NULL CHECK (selected_knowledge_base IN ('main','cooking')),
    ingestion_status text NOT NULL CHECK (ingestion_status IN ('pending','partial','completed','failed')),
    telegram_chat_id text NOT NULL,
    telegram_message_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, selected_knowledge_base)
);

COMMIT;
