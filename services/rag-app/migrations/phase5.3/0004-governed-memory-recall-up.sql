\set ON_ERROR_STOP on

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

SELECT pg_advisory_xact_lock(74518, 530004);

DO $phase53$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM public.rag_schema_migrations
        WHERE version = 'phase5.2-0003'
    ) THEN
        RAISE EXCEPTION
            'Required migration phase5.2-0003 is not recorded';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.rag_schema_migrations
        WHERE version = 'phase5.3-0004'
    ) THEN
        RAISE EXCEPTION
            'Migration phase5.3-0004 is already recorded';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = 'rag_memory_api_owner'
          AND NOT rolcanlogin
          AND NOT rolsuper
    ) OR NOT EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = 'rag_mcp_runtime'
          AND NOT rolsuper
    ) THEN
        RAISE EXCEPTION
            'Phase 5.2 database role boundary is incomplete';
    END IF;

    IF to_regprocedure(
        'public.rag_api_memory_embedding_succeed(uuid,uuid,integer,bigint,text,integer,vector,text,text,text)'
    ) IS NOT NULL
       OR to_regprocedure(
        'public.rag_api_memory_search(vector,integer)'
    ) IS NOT NULL
       OR to_regprocedure(
        'public.rag_api_memory_restore_ready(uuid,integer,bigint,text,text,text,text)'
    ) IS NOT NULL THEN
        RAISE EXCEPTION
            'Phase 5.3 recall routines already exist unexpectedly';
    END IF;
END
$phase53$;

GRANT UPDATE (
    embedding_status,
    embedding_model,
    embedding_dimensions,
    embedding
)
ON TABLE public.rag_memory_revisions
TO rag_memory_api_owner;

CREATE OR REPLACE FUNCTION public.rag_reject_immutable_change()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF TG_TABLE_NAME = 'rag_memory_revisions'
       AND TG_OP = 'UPDATE' THEN
        IF current_setting(
            'rag.embedding_update_authorized',
            TRUE
        ) = 'on'
           AND (
               to_jsonb(NEW) - ARRAY[
                   'embedding_status',
                   'embedding_model',
                   'embedding_dimensions',
                   'embedding'
               ]
           ) = (
               to_jsonb(OLD) - ARRAY[
                   'embedding_status',
                   'embedding_model',
                   'embedding_dimensions',
                   'embedding'
               ]
           )
           AND OLD.embedding_status IN ('pending', 'failed')
           AND NEW.embedding_status = 'ready' THEN
            RETURN NEW;
        END IF;
    END IF;

    RAISE EXCEPTION
        '% is immutable; % is not permitted',
        TG_TABLE_NAME,
        TG_OP;
END
$function$;

CREATE FUNCTION public.rag_api_memory_embedding_succeed(
    p_memory_id UUID,
    p_revision_id UUID,
    p_expected_version INTEGER,
    p_expected_state_version BIGINT,
    p_embedding_model TEXT,
    p_embedding_dimensions INTEGER,
    p_embedding VECTOR(1024),
    p_idempotency_key TEXT,
    p_request_hash TEXT,
    p_source_ref TEXT
)
RETURNS TABLE (
    memory_id UUID,
    revision_id UUID,
    event_id BIGINT,
    embedding_status TEXT,
    replayed BOOLEAN
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    v_actor CONSTANT TEXT := 'openclaw-service:rag-mcp';
    v_memory public.rag_memories%ROWTYPE;
    v_event public.rag_memory_events%ROWTYPE;
    v_content_hash TEXT;
    v_old_embedding_status TEXT;
    v_event_id BIGINT;
BEGIN
    IF p_memory_id IS NULL
       OR p_revision_id IS NULL
       OR p_expected_version IS NULL
       OR p_expected_version < 1
       OR p_expected_state_version IS NULL
       OR p_expected_state_version < 1
       OR p_embedding_model <> 'qwen3-embedding:0.6b'
       OR p_embedding_dimensions <> 1024
       OR p_embedding IS NULL
       OR vector_dims(p_embedding) <> 1024 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: embedding input is invalid';
    END IF;

    PERFORM public.rag_api_validate_common(
        'Generate current revision embedding',
        p_source_ref,
        p_idempotency_key,
        p_request_hash,
        '{}'::JSONB
    );

    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            'phase5.3:' || p_idempotency_key,
            530004
        )
    );

    SELECT event_record.*
    INTO v_event
    FROM public.rag_memory_events AS event_record
    WHERE event_record.idempotency_key = p_idempotency_key;

    IF FOUND THEN
        IF v_event.event_type <> 'embedding_succeeded'
           OR v_event.memory_id <> p_memory_id
           OR v_event.revision_id <> p_revision_id
           OR v_event.new_state ->> 'request_hash'
                IS DISTINCT FROM p_request_hash THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P5207',
                MESSAGE = 'idempotency_conflict';
        END IF;

        RETURN QUERY
        SELECT
            v_event.memory_id,
            v_event.revision_id,
            v_event.id,
            'ready'::TEXT,
            TRUE;

        RETURN;
    END IF;

    SELECT memory_record.*
    INTO v_memory
    FROM public.rag_memories AS memory_record
    WHERE memory_record.id = p_memory_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5202',
            MESSAGE = 'not_found';
    END IF;

    IF v_memory.status IN ('archived', 'rejected')
       OR v_memory.current_version <> p_expected_version
       OR v_memory.state_version <> p_expected_state_version
       OR v_memory.current_revision_id <> p_revision_id THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5203',
            MESSAGE = 'concurrency_conflict';
    END IF;

    SELECT
        revision_record.content_hash,
        revision_record.embedding_status
    INTO
        v_content_hash,
        v_old_embedding_status
    FROM public.rag_memory_revisions AS revision_record
    WHERE revision_record.id = p_revision_id
      AND revision_record.memory_id = p_memory_id
      AND revision_record.version = p_expected_version;

    IF NOT FOUND OR v_old_embedding_status NOT IN ('pending', 'failed') THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5204',
            MESSAGE = 'invalid_state: revision is not embeddable';
    END IF;

    PERFORM set_config(
        'rag.embedding_update_authorized',
        'on',
        TRUE
    );

    UPDATE public.rag_memory_revisions AS revision_record
    SET
        embedding_status = 'ready',
        embedding_model = p_embedding_model,
        embedding_dimensions = p_embedding_dimensions,
        embedding = p_embedding
    WHERE revision_record.id = p_revision_id
      AND revision_record.memory_id = p_memory_id;

    PERFORM set_config(
        'rag.embedding_update_authorized',
        'off',
        TRUE
    );

    INSERT INTO public.rag_memory_events (
        memory_id,
        revision_id,
        event_type,
        actor,
        source_ref,
        reason,
        idempotency_key,
        old_state,
        new_state
    )
    VALUES (
        p_memory_id,
        p_revision_id,
        'embedding_succeeded',
        v_actor,
        p_source_ref,
        'Generate current revision embedding',
        p_idempotency_key,
        jsonb_build_object(
            'operation', 'embedding_succeeded',
            'current_version', p_expected_version,
            'state_version', p_expected_state_version,
            'revision_id', p_revision_id,
            'content_hash', v_content_hash,
            'embedding_status', v_old_embedding_status
        ),
        jsonb_build_object(
            'operation', 'embedding_succeeded',
            'request_hash', p_request_hash,
            'current_version', p_expected_version,
            'state_version', p_expected_state_version,
            'revision_id', p_revision_id,
            'content_hash', v_content_hash,
            'embedding_status', 'ready',
            'embedding_model', p_embedding_model,
            'embedding_dimensions', p_embedding_dimensions
        )
    )
    RETURNING id INTO v_event_id;

    RETURN QUERY
    SELECT
        p_memory_id,
        p_revision_id,
        v_event_id,
        'ready'::TEXT,
        FALSE;
END
$function$;

CREATE FUNCTION public.rag_api_memory_search(
    p_query_embedding VECTOR(1024),
    p_limit INTEGER
)
RETURNS TABLE (
    memory_id UUID,
    namespace TEXT,
    memory_type TEXT,
    current_version INTEGER,
    content TEXT,
    content_truncated BOOLEAN,
    source_type TEXT,
    source_ref TEXT,
    trust_level TEXT,
    metadata JSONB,
    cosine_similarity DOUBLE PRECISION
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF p_query_embedding IS NULL
       OR vector_dims(p_query_embedding) <> 1024
       OR p_limit IS NULL
       OR p_limit NOT BETWEEN 1 AND 10 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: search input is invalid';
    END IF;

    RETURN QUERY
    SELECT
        memory_record.id,
        memory_record.namespace,
        memory_record.memory_type,
        memory_record.current_version,
        left(revision_record.content, 4000),
        length(revision_record.content) > 4000,
        revision_record.source_type,
        revision_record.source_ref,
        revision_record.trust_level,
        revision_record.metadata,
        (
            1 - (
                revision_record.embedding <=> p_query_embedding
            )
        )::DOUBLE PRECISION
    FROM public.rag_memories AS memory_record
    JOIN public.rag_memory_revisions AS revision_record
      ON revision_record.id = memory_record.current_revision_id
     AND revision_record.memory_id = memory_record.id
     AND revision_record.version = memory_record.current_version
    WHERE memory_record.status = 'active'
      AND revision_record.embedding_status = 'ready'
      AND revision_record.embedding_model = 'qwen3-embedding:0.6b'
      AND revision_record.embedding_dimensions = 1024
      AND revision_record.embedding IS NOT NULL
    ORDER BY
        revision_record.embedding <=> p_query_embedding,
        memory_record.updated_at DESC,
        memory_record.id
    LIMIT p_limit;
END
$function$;

CREATE FUNCTION public.rag_api_memory_restore_ready(
    p_memory_id UUID,
    p_expected_version INTEGER,
    p_expected_state_version BIGINT,
    p_reason TEXT,
    p_idempotency_key TEXT,
    p_request_hash TEXT,
    p_source_ref TEXT
)
RETURNS TABLE (
    operation TEXT,
    memory_id UUID,
    revision_id UUID,
    event_id BIGINT,
    current_version INTEGER,
    state_version BIGINT,
    status TEXT,
    content_hash TEXT,
    replayed BOOLEAN
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    v_embedding_status TEXT;
BEGIN
    SELECT revision_record.embedding_status
    INTO v_embedding_status
    FROM public.rag_memories AS memory_record
    JOIN public.rag_memory_revisions AS revision_record
      ON revision_record.id = memory_record.current_revision_id
     AND revision_record.memory_id = memory_record.id
    WHERE memory_record.id = p_memory_id
      AND memory_record.current_version = p_expected_version
      AND memory_record.state_version = p_expected_state_version;

    IF FOUND AND v_embedding_status <> 'ready' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5204',
            MESSAGE = 'invalid_state: current revision is not searchable';
    END IF;

    RETURN QUERY
    SELECT result.*
    FROM public.rag_api_memory_restore(
        p_memory_id,
        p_expected_version,
        p_expected_state_version,
        p_reason,
        p_idempotency_key,
        p_request_hash,
        p_source_ref
    ) AS result;
END
$function$;

ALTER FUNCTION public.rag_api_memory_embedding_succeed(
    UUID,
    UUID,
    INTEGER,
    BIGINT,
    TEXT,
    INTEGER,
    VECTOR,
    TEXT,
    TEXT,
    TEXT
)
    OWNER TO rag_memory_api_owner;

ALTER FUNCTION public.rag_api_memory_search(VECTOR, INTEGER)
    OWNER TO rag_memory_api_owner;

ALTER FUNCTION public.rag_api_memory_restore_ready(
    UUID,
    INTEGER,
    BIGINT,
    TEXT,
    TEXT,
    TEXT,
    TEXT
)
    OWNER TO rag_memory_api_owner;

REVOKE ALL
ON FUNCTION public.rag_api_memory_embedding_succeed(
    UUID,
    UUID,
    INTEGER,
    BIGINT,
    TEXT,
    INTEGER,
    VECTOR,
    TEXT,
    TEXT,
    TEXT
)
FROM PUBLIC;

REVOKE ALL
ON FUNCTION public.rag_api_memory_search(VECTOR, INTEGER)
FROM PUBLIC;

REVOKE ALL
ON FUNCTION public.rag_api_memory_restore_ready(
    UUID,
    INTEGER,
    BIGINT,
    TEXT,
    TEXT,
    TEXT,
    TEXT
)
FROM PUBLIC;

REVOKE EXECUTE
ON FUNCTION public.rag_api_memory_restore(
    UUID,
    INTEGER,
    BIGINT,
    TEXT,
    TEXT,
    TEXT,
    TEXT
)
FROM rag_mcp_runtime;

GRANT EXECUTE
ON FUNCTION public.rag_api_memory_embedding_succeed(
    UUID,
    UUID,
    INTEGER,
    BIGINT,
    TEXT,
    INTEGER,
    VECTOR,
    TEXT,
    TEXT,
    TEXT
)
TO rag_mcp_runtime;

GRANT EXECUTE
ON FUNCTION public.rag_api_memory_search(VECTOR, INTEGER)
TO rag_mcp_runtime;

GRANT EXECUTE
ON FUNCTION public.rag_api_memory_restore_ready(
    UUID,
    INTEGER,
    BIGINT,
    TEXT,
    TEXT,
    TEXT,
    TEXT
)
TO rag_mcp_runtime;

INSERT INTO public.rag_schema_migrations (
    version,
    description
)
VALUES (
    'phase5.3-0004',
    'Add governed memory embedding completion and active-current search API'
);

COMMIT;
