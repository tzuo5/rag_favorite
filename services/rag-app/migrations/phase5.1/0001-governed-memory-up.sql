\set ON_ERROR_STOP on

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

SELECT pg_advisory_xact_lock(74518, 510001);

DO $$
BEGIN
    IF to_regclass('public.rag_memories') IS NOT NULL
       OR to_regclass('public.rag_memory_revisions') IS NOT NULL
       OR to_regclass('public.rag_memory_events') IS NOT NULL THEN
        RAISE EXCEPTION
            'Phase 5.1 target tables already exist; refusing non-idempotent migration';
    END IF;

    IF to_regprocedure('public.rag_validate_revision_chain()') IS NOT NULL
       OR to_regprocedure('public.rag_reject_immutable_change()') IS NOT NULL THEN
        RAISE EXCEPTION
            'Phase 5.1 trigger functions already exist unexpectedly';
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS public.rag_schema_migrations (
    version TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT rag_schema_migrations_version_not_blank
        CHECK (btrim(version) <> ''),

    CONSTRAINT rag_schema_migrations_description_not_blank
        CHECK (btrim(description) <> '')
);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.rag_schema_migrations
        WHERE version = 'phase5.1-0001'
    ) THEN
        RAISE EXCEPTION 'Migration phase5.1-0001 is already recorded';
    END IF;
END
$$;

CREATE TABLE public.rag_memories (
    id UUID PRIMARY KEY,

    namespace TEXT NOT NULL,
    memory_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate',

    current_version INTEGER NOT NULL,
    current_revision_id UUID NOT NULL,

    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    archived_at TIMESTAMPTZ,
    archived_by TEXT,
    archive_reason TEXT,

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT rag_memories_namespace_allowed
        CHECK (
            namespace IN (
                'personal_memory',
                'project_memory',
                'knowledge_documents',
                'agent_observations',
                'quarantine'
            )
        ),

    CONSTRAINT rag_memories_memory_type_not_blank
        CHECK (btrim(memory_type) <> ''),

    CONSTRAINT rag_memories_memory_type_length
        CHECK (length(memory_type) <= 128),

    CONSTRAINT rag_memories_status_allowed
        CHECK (
            status IN (
                'candidate',
                'active',
                'archived',
                'rejected'
            )
        ),

    CONSTRAINT rag_memories_current_version_positive
        CHECK (current_version >= 1),

    CONSTRAINT rag_memories_created_by_not_blank
        CHECK (btrim(created_by) <> ''),

    CONSTRAINT rag_memories_updated_by_not_blank
        CHECK (btrim(updated_by) <> ''),

    CONSTRAINT rag_memories_actor_lengths
        CHECK (
            length(created_by) <= 255
            AND length(updated_by) <= 255
        ),

    CONSTRAINT rag_memories_timestamp_order
        CHECK (updated_at >= created_at),

    CONSTRAINT rag_memories_archive_state_consistent
        CHECK (
            (
                status = 'archived'
                AND archived_at IS NOT NULL
                AND archived_by IS NOT NULL
                AND btrim(archived_by) <> ''
                AND archive_reason IS NOT NULL
                AND btrim(archive_reason) <> ''
            )
            OR
            (
                status <> 'archived'
                AND archived_at IS NULL
                AND archived_by IS NULL
                AND archive_reason IS NULL
            )
        ),

    CONSTRAINT rag_memories_archive_actor_length
        CHECK (
            archived_by IS NULL
            OR length(archived_by) <= 255
        ),

    CONSTRAINT rag_memories_metadata_object
        CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE TABLE public.rag_memory_revisions (
    id UUID PRIMARY KEY,
    memory_id UUID NOT NULL,

    version INTEGER NOT NULL,

    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,

    source_type TEXT NOT NULL,
    source_ref TEXT,
    trust_level TEXT NOT NULL,

    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    supersedes_revision_id UUID,

    embedding_status TEXT NOT NULL DEFAULT 'pending',
    embedding_model TEXT,
    embedding_dimensions INTEGER,
    embedding VECTOR(1024),

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT rag_memory_revisions_memory_fk
        FOREIGN KEY (memory_id)
        REFERENCES public.rag_memories(id)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,

    CONSTRAINT rag_memory_revisions_supersedes_fk
        FOREIGN KEY (supersedes_revision_id)
        REFERENCES public.rag_memory_revisions(id)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,

    CONSTRAINT rag_memory_revisions_memory_version_unique
        UNIQUE (memory_id, version),

    CONSTRAINT rag_memory_revisions_current_reference_unique
        UNIQUE (memory_id, version, id),

    CONSTRAINT rag_memory_revisions_memory_id_id_unique
        UNIQUE (memory_id, id),

    CONSTRAINT rag_memory_revisions_version_positive
        CHECK (version >= 1),

    CONSTRAINT rag_memory_revisions_content_not_blank
        CHECK (btrim(content) <> ''),

    CONSTRAINT rag_memory_revisions_content_hash_sha256
        CHECK (content_hash ~ '^[0-9a-f]{64}$'),

    CONSTRAINT rag_memory_revisions_source_type_allowed
        CHECK (
            source_type IN (
                'user_explicit',
                'user_implicit',
                'agent_inference',
                'external_document',
                'tool_output',
                'system_generated'
            )
        ),

    CONSTRAINT rag_memory_revisions_source_ref_length
        CHECK (
            source_ref IS NULL
            OR length(source_ref) <= 2048
        ),

    CONSTRAINT rag_memory_revisions_trust_level_allowed
        CHECK (
            trust_level IN (
                'untrusted',
                'low',
                'medium',
                'high'
            )
        ),

    CONSTRAINT rag_memory_revisions_reason_not_blank
        CHECK (btrim(reason) <> ''),

    CONSTRAINT rag_memory_revisions_created_by_not_blank
        CHECK (btrim(created_by) <> ''),

    CONSTRAINT rag_memory_revisions_created_by_length
        CHECK (length(created_by) <= 255),

    CONSTRAINT rag_memory_revisions_embedding_status_allowed
        CHECK (
            embedding_status IN (
                'pending',
                'ready',
                'failed',
                'not_required'
            )
        ),

    CONSTRAINT rag_memory_revisions_embedding_consistent
        CHECK (
            (
                embedding_status = 'ready'
                AND embedding_model IS NOT NULL
                AND btrim(embedding_model) <> ''
                AND embedding_dimensions = 1024
                AND embedding IS NOT NULL
            )
            OR
            (
                embedding_status <> 'ready'
                AND embedding IS NULL
                AND (
                    embedding_dimensions IS NULL
                    OR embedding_dimensions = 1024
                )
            )
        ),

    CONSTRAINT rag_memory_revisions_embedding_model_length
        CHECK (
            embedding_model IS NULL
            OR length(embedding_model) <= 255
        ),

    CONSTRAINT rag_memory_revisions_metadata_object
        CHECK (jsonb_typeof(metadata) = 'object')
);

ALTER TABLE public.rag_memories
    ADD CONSTRAINT rag_memories_current_revision_fk
    FOREIGN KEY (id, current_version, current_revision_id)
    REFERENCES public.rag_memory_revisions(memory_id, version, id)
    ON DELETE RESTRICT
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE public.rag_memory_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    memory_id UUID NOT NULL,
    revision_id UUID,

    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    source_ref TEXT,
    reason TEXT NOT NULL,

    idempotency_key TEXT NOT NULL,

    old_state JSONB,
    new_state JSONB,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT rag_memory_events_memory_fk
        FOREIGN KEY (memory_id)
        REFERENCES public.rag_memories(id)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,

    CONSTRAINT rag_memory_events_revision_memory_fk
        FOREIGN KEY (memory_id, revision_id)
        REFERENCES public.rag_memory_revisions(memory_id, id)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,

    CONSTRAINT rag_memory_events_event_type_allowed
        CHECK (
            event_type IN (
                'create',
                'update',
                'archive',
                'restore',
                'promote',
                'reject',
                'embedding_succeeded',
                'embedding_failed'
            )
        ),

    CONSTRAINT rag_memory_events_actor_not_blank
        CHECK (btrim(actor) <> ''),

    CONSTRAINT rag_memory_events_actor_length
        CHECK (length(actor) <= 255),

    CONSTRAINT rag_memory_events_source_ref_length
        CHECK (
            source_ref IS NULL
            OR length(source_ref) <= 2048
        ),

    CONSTRAINT rag_memory_events_reason_not_blank
        CHECK (btrim(reason) <> ''),

    CONSTRAINT rag_memory_events_idempotency_key_not_blank
        CHECK (btrim(idempotency_key) <> ''),

    CONSTRAINT rag_memory_events_idempotency_key_length
        CHECK (length(idempotency_key) <= 255),

    CONSTRAINT rag_memory_events_old_state_object
        CHECK (
            old_state IS NULL
            OR jsonb_typeof(old_state) = 'object'
        ),

    CONSTRAINT rag_memory_events_new_state_object
        CHECK (
            new_state IS NULL
            OR jsonb_typeof(new_state) = 'object'
        ),

    CONSTRAINT rag_memory_events_old_state_no_content
        CHECK (
            old_state IS NULL
            OR NOT (
                old_state ?| ARRAY[
                    'content',
                    'old_content',
                    'new_content',
                    'embedding'
                ]
            )
        ),

    CONSTRAINT rag_memory_events_new_state_no_content
        CHECK (
            new_state IS NULL
            OR NOT (
                new_state ?| ARRAY[
                    'content',
                    'old_content',
                    'new_content',
                    'embedding'
                ]
            )
        ),

    CONSTRAINT rag_memory_events_idempotency_unique
        UNIQUE (idempotency_key)
);

CREATE FUNCTION public.rag_validate_revision_chain()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    previous_memory_id UUID;
    previous_version INTEGER;
BEGIN
    IF NEW.version = 1 THEN
        IF NEW.supersedes_revision_id IS NOT NULL THEN
            RAISE EXCEPTION
                'Version 1 revision must not supersede another revision';
        END IF;

        RETURN NEW;
    END IF;

    IF NEW.supersedes_revision_id IS NULL THEN
        RAISE EXCEPTION
            'Revision version % must identify the immediately preceding revision',
            NEW.version;
    END IF;

    SELECT memory_id, version
    INTO previous_memory_id, previous_version
    FROM public.rag_memory_revisions
    WHERE id = NEW.supersedes_revision_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'Superseded revision % does not exist',
            NEW.supersedes_revision_id;
    END IF;

    IF previous_memory_id <> NEW.memory_id THEN
        RAISE EXCEPTION
            'Superseded revision belongs to a different memory';
    END IF;

    IF previous_version <> NEW.version - 1 THEN
        RAISE EXCEPTION
            'Revision version % must supersede version %, not version %',
            NEW.version,
            NEW.version - 1,
            previous_version;
    END IF;

    RETURN NEW;
END
$$;

CREATE TRIGGER rag_memory_revisions_validate_chain
BEFORE INSERT ON public.rag_memory_revisions
FOR EACH ROW
EXECUTE FUNCTION public.rag_validate_revision_chain();

CREATE FUNCTION public.rag_reject_immutable_change()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        '% is immutable; % is not permitted',
        TG_TABLE_NAME,
        TG_OP;
END
$$;

CREATE TRIGGER rag_memory_revisions_reject_update
BEFORE UPDATE ON public.rag_memory_revisions
FOR EACH ROW
EXECUTE FUNCTION public.rag_reject_immutable_change();

CREATE TRIGGER rag_memory_revisions_reject_delete
BEFORE DELETE ON public.rag_memory_revisions
FOR EACH ROW
EXECUTE FUNCTION public.rag_reject_immutable_change();

CREATE TRIGGER rag_memory_events_reject_update
BEFORE UPDATE ON public.rag_memory_events
FOR EACH ROW
EXECUTE FUNCTION public.rag_reject_immutable_change();

CREATE TRIGGER rag_memory_events_reject_delete
BEFORE DELETE ON public.rag_memory_events
FOR EACH ROW
EXECUTE FUNCTION public.rag_reject_immutable_change();

CREATE INDEX rag_memories_status_namespace_idx
    ON public.rag_memories(status, namespace);

CREATE INDEX rag_memories_namespace_type_status_idx
    ON public.rag_memories(namespace, memory_type, status);

CREATE INDEX rag_memory_revisions_memory_version_desc_idx
    ON public.rag_memory_revisions(memory_id, version DESC);

CREATE INDEX rag_memory_revisions_content_hash_idx
    ON public.rag_memory_revisions(content_hash);

CREATE INDEX rag_memory_events_memory_created_desc_idx
    ON public.rag_memory_events(memory_id, created_at DESC, id DESC);

INSERT INTO public.rag_schema_migrations (
    version,
    description
)
VALUES (
    'phase5.1-0001',
    'Add governed memory identity, immutable revision history and audit events'
);

COMMIT;
