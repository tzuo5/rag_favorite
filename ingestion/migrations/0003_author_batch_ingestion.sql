BEGIN;

ALTER TABLE video_ingestion_jobs
    ADD COLUMN IF NOT EXISTS notification_mode text NOT NULL DEFAULT 'INDIVIDUAL',
    ADD COLUMN IF NOT EXISTS destination_locked boolean NOT NULL DEFAULT false;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'video_ingestion_jobs'::regclass
          AND conname = 'video_ingestion_jobs_notification_mode_check'
    ) THEN
        ALTER TABLE video_ingestion_jobs
            ADD CONSTRAINT video_ingestion_jobs_notification_mode_check
            CHECK (notification_mode IN ('INDIVIDUAL', 'BATCH_SILENT'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'video_ingestion_jobs'::regclass
          AND conname = 'video_ingestion_jobs_locked_destination_check'
    ) THEN
        ALTER TABLE video_ingestion_jobs
            ADD CONSTRAINT video_ingestion_jobs_locked_destination_check
            CHECK (NOT destination_locked OR selected_destination IS NOT NULL);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS video_author_discoveries (
    id uuid PRIMARY KEY,
    telegram_user_id text NOT NULL,
    telegram_chat_id text NOT NULL,
    telegram_message_id text NOT NULL,
    input_url text NOT NULL,
    platform text NOT NULL CHECK (
        platform IN ('youtube', 'bilibili', 'xiaohongshu')
    ),
    author_id text,
    canonical_author_url text NOT NULL,
    author_name text,
    state text NOT NULL CHECK (state IN (
        'QUEUED', 'DISCOVERING', 'READY', 'FAILED', 'EXPIRED', 'CONSUMED'
    )),
    scan_limit integer NOT NULL CHECK (scan_limit > 0),
    discovered_count integer NOT NULL DEFAULT 0 CHECK (discovered_count >= 0),
    eligible_count integer NOT NULL DEFAULT 0 CHECK (
        eligible_count >= 0 AND eligible_count <= discovered_count
    ),
    existing_main_count integer NOT NULL DEFAULT 0 CHECK (
        existing_main_count >= 0 AND existing_main_count <= discovered_count
    ),
    existing_cooking_count integer NOT NULL DEFAULT 0 CHECK (
        existing_cooking_count >= 0 AND existing_cooking_count <= discovered_count
    ),
    error_code text,
    error_message text,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    CONSTRAINT video_author_discoveries_author_identity_check CHECK (
        state NOT IN ('READY', 'CONSUMED') OR author_id IS NOT NULL
    ),
    CONSTRAINT video_author_discoveries_expiry_check CHECK (expires_at > created_at),
    CONSTRAINT video_author_discoveries_finished_check CHECK (
        state NOT IN ('FAILED', 'EXPIRED', 'CONSUMED') OR finished_at IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS video_author_discoveries_user_created_idx
    ON video_author_discoveries (telegram_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS video_author_discoveries_state_expiry_idx
    ON video_author_discoveries (state, expires_at);
CREATE INDEX IF NOT EXISTS video_author_discoveries_author_idx
    ON video_author_discoveries (platform, author_id);
CREATE UNIQUE INDEX IF NOT EXISTS video_author_discoveries_active_key
    ON video_author_discoveries (
        telegram_user_id, platform, canonical_author_url
    )
    WHERE state IN ('QUEUED', 'DISCOVERING');

CREATE TABLE IF NOT EXISTS video_author_discovery_items (
    id bigserial PRIMARY KEY,
    discovery_id uuid NOT NULL REFERENCES video_author_discoveries(id) ON DELETE CASCADE,
    platform text NOT NULL CHECK (
        platform IN ('youtube', 'bilibili', 'xiaohongshu')
    ),
    source_id text NOT NULL,
    canonical_url text NOT NULL,
    title text,
    published_at timestamptz,
    duration_seconds double precision CHECK (
        duration_seconds IS NULL OR duration_seconds >= 0
    ),
    content_type text NOT NULL CHECK (
        content_type IN ('VIDEO', 'SHORT', 'LIVE_REPLAY', 'UNKNOWN')
    ),
    position integer NOT NULL CHECK (position > 0),
    eligibility text NOT NULL CHECK (
        eligibility IN ('ELIGIBLE', 'UNAVAILABLE', 'UNSUPPORTED')
    ),
    raw_metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(raw_metadata) = 'object'
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (discovery_id, platform, source_id),
    UNIQUE (discovery_id, position)
);

CREATE INDEX IF NOT EXISTS video_author_discovery_items_selection_idx
    ON video_author_discovery_items (discovery_id, eligibility, position);

CREATE TABLE IF NOT EXISTS video_ingestion_batches (
    id uuid PRIMARY KEY,
    discovery_id uuid NOT NULL UNIQUE
        REFERENCES video_author_discoveries(id) ON DELETE RESTRICT,
    telegram_user_id text NOT NULL,
    telegram_chat_id text NOT NULL,
    telegram_message_id text NOT NULL,
    platform text NOT NULL CHECK (
        platform IN ('youtube', 'bilibili', 'xiaohongshu')
    ),
    author_id text NOT NULL,
    author_url text NOT NULL,
    author_name text,
    selection_policy jsonb NOT NULL CHECK (
        jsonb_typeof(selection_policy) = 'object'
    ),
    selected_destination text NOT NULL CHECK (
        selected_destination IN ('main', 'cooking')
    ),
    state text NOT NULL CHECK (state IN (
        'QUEUED', 'RUNNING', 'PAUSED_USER', 'PAUSED_AUTH',
        'PAUSED_RATE_LIMIT', 'PAUSED_RESOURCE', 'CANCELLING',
        'COMPLETED', 'COMPLETED_WITH_ERRORS', 'CANCELLED', 'FAILED'
    )),
    total_count integer NOT NULL CHECK (total_count BETWEEN 1 AND 50),
    queued_count integer NOT NULL DEFAULT 0 CHECK (queued_count >= 0),
    running_count integer NOT NULL DEFAULT 0 CHECK (running_count >= 0),
    completed_count integer NOT NULL DEFAULT 0 CHECK (completed_count >= 0),
    skipped_existing_count integer NOT NULL DEFAULT 0 CHECK (
        skipped_existing_count >= 0
    ),
    failed_count integer NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    cancelled_count integer NOT NULL DEFAULT 0 CHECK (cancelled_count >= 0),
    pause_code text,
    pause_message text,
    resume_not_before timestamptz,
    cancel_requested_at timestamptz,
    last_notified_at timestamptz,
    notified_terminal_count integer NOT NULL DEFAULT 0 CHECK (
        notified_terminal_count >= 0
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    CONSTRAINT video_ingestion_batches_finished_check CHECK (
        (state IN ('COMPLETED', 'COMPLETED_WITH_ERRORS', 'CANCELLED', 'FAILED'))
        = (finished_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS video_ingestion_batches_state_updated_idx
    ON video_ingestion_batches (state, updated_at);
CREATE INDEX IF NOT EXISTS video_ingestion_batches_user_created_idx
    ON video_ingestion_batches (telegram_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS video_ingestion_batches_author_created_idx
    ON video_ingestion_batches (platform, author_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS video_ingestion_batches_active_user_key
    ON video_ingestion_batches (telegram_user_id)
    WHERE state IN (
        'QUEUED', 'RUNNING', 'PAUSED_USER', 'PAUSED_AUTH',
        'PAUSED_RATE_LIMIT', 'PAUSED_RESOURCE', 'CANCELLING'
    );

CREATE TABLE IF NOT EXISTS video_ingestion_batch_items (
    id uuid PRIMARY KEY,
    batch_id uuid NOT NULL
        REFERENCES video_ingestion_batches(id) ON DELETE RESTRICT,
    platform text NOT NULL CHECK (
        platform IN ('youtube', 'bilibili', 'xiaohongshu')
    ),
    source_id text NOT NULL,
    canonical_url text NOT NULL,
    title text,
    published_at timestamptz,
    duration_seconds double precision CHECK (
        duration_seconds IS NULL OR duration_seconds >= 0
    ),
    content_type text NOT NULL CHECK (
        content_type IN ('VIDEO', 'SHORT', 'LIVE_REPLAY', 'UNKNOWN')
    ),
    position integer NOT NULL CHECK (position > 0),
    state text NOT NULL CHECK (state IN (
        'PENDING', 'QUEUED', 'RUNNING', 'COMPLETED',
        'SKIPPED_EXISTING', 'FAILED', 'CANCELLED'
    )),
    job_id uuid UNIQUE
        REFERENCES video_ingestion_jobs(id) ON DELETE RESTRICT,
    existing_document_record_id bigint
        REFERENCES video_knowledge_documents(record_id) ON DELETE RESTRICT,
    error_code text,
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    queued_at timestamptz,
    started_at timestamptz,
    finished_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (batch_id, platform, source_id),
    UNIQUE (batch_id, position),
    CONSTRAINT video_ingestion_batch_items_binding_check CHECK (
        (
            state = 'PENDING'
            AND job_id IS NULL
            AND existing_document_record_id IS NULL
        )
        OR (
            state IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')
            AND job_id IS NOT NULL
            AND existing_document_record_id IS NULL
        )
        OR (
            state = 'CANCELLED'
            AND existing_document_record_id IS NULL
        )
        OR (
            state = 'SKIPPED_EXISTING'
            AND job_id IS NULL
            AND existing_document_record_id IS NOT NULL
        )
    ),
    CONSTRAINT video_ingestion_batch_items_finished_check CHECK (
        (state IN ('COMPLETED', 'SKIPPED_EXISTING', 'FAILED', 'CANCELLED'))
        = (finished_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS video_ingestion_batch_items_state_position_idx
    ON video_ingestion_batch_items (batch_id, state, position);
CREATE INDEX IF NOT EXISTS video_ingestion_batch_items_source_idx
    ON video_ingestion_batch_items (platform, source_id);

CREATE TABLE IF NOT EXISTS video_platform_request_gates (
    platform text PRIMARY KEY CHECK (
        platform IN ('youtube', 'bilibili', 'xiaohongshu')
    ),
    circuit_state text NOT NULL DEFAULT 'CLOSED' CHECK (
        circuit_state IN ('CLOSED', 'OPEN', 'HALF_OPEN')
    ),
    next_allowed_at timestamptz NOT NULL DEFAULT now(),
    blocked_until timestamptz,
    consecutive_failures integer NOT NULL DEFAULT 0 CHECK (
        consecutive_failures >= 0
    ),
    last_error_code text,
    probe_in_flight boolean NOT NULL DEFAULT false,
    opened_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_success_at timestamptz
);

CREATE INDEX IF NOT EXISTS video_platform_request_gates_schedule_idx
    ON video_platform_request_gates (
        circuit_state, blocked_until, next_allowed_at
    );

CREATE TABLE IF NOT EXISTS video_ingestion_batch_notifications (
    id uuid PRIMARY KEY,
    batch_id uuid NOT NULL
        REFERENCES video_ingestion_batches(id) ON DELETE RESTRICT,
    event_type text NOT NULL CHECK (event_type IN (
        'CREATED', 'PROGRESS', 'PAUSED', 'RESUMED', 'CANCELLING', 'TERMINAL'
    )),
    idempotency_key text NOT NULL UNIQUE,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 100),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS video_ingestion_batch_notifications_pending_idx
    ON video_ingestion_batch_notifications (next_attempt_at, created_at)
    WHERE delivered_at IS NULL;
CREATE INDEX IF NOT EXISTS video_ingestion_batch_notifications_batch_idx
    ON video_ingestion_batch_notifications (batch_id, created_at);

COMMIT;
