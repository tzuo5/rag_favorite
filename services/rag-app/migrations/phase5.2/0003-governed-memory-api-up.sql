\set ON_ERROR_STOP on

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

SELECT pg_advisory_xact_lock(74518, 520003);

DO $phase52$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM public.rag_schema_migrations
        WHERE version = 'phase5.2-0002'
    ) THEN
        RAISE EXCEPTION
            'Required migration phase5.2-0002 is not recorded';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.rag_schema_migrations
        WHERE version = 'phase5.2-0003'
    ) THEN
        RAISE EXCEPTION
            'Migration phase5.2-0003 is already recorded';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'rag_memories'
          AND column_name = 'state_version'
          AND data_type = 'bigint'
          AND is_nullable = 'NO'
    ) THEN
        RAISE EXCEPTION
            'Required rag_memories.state_version column is missing';
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

    IF EXISTS (
        SELECT 1
        FROM pg_proc AS procedure
        JOIN pg_namespace AS namespace
          ON namespace.oid = procedure.pronamespace
        WHERE namespace.nspname = 'public'
          AND procedure.proname LIKE 'rag_api_%'
    ) THEN
        RAISE EXCEPTION
            'Phase 5.2 API routines already exist unexpectedly';
    END IF;
END
$phase52$;

CREATE FUNCTION public.rag_api_validate_metadata(
    p_metadata JSONB
)
RETURNS VOID
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF p_metadata IS NULL
       OR jsonb_typeof(p_metadata) <> 'object' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: metadata must be an object';
    END IF;

    IF octet_length(p_metadata::TEXT) > 4096 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: metadata exceeds size limit';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM jsonb_object_keys(p_metadata) AS key_name
        WHERE key_name NOT IN (
            'tags',
            'project',
            'conversation_ref',
            'source_title',
            'language'
        )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: metadata contains an unknown key';
    END IF;

    IF p_metadata ? 'tags' THEN
        IF jsonb_typeof(p_metadata -> 'tags') <> 'array'
           OR jsonb_array_length(p_metadata -> 'tags') > 20 THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P5201',
                MESSAGE = 'validation_error: metadata tags are invalid';
        END IF;

        IF EXISTS (
            SELECT 1
            FROM jsonb_array_elements(
                p_metadata -> 'tags'
            ) AS tag_value
            WHERE jsonb_typeof(tag_value) <> 'string'
               OR length(tag_value #>> '{}') NOT BETWEEN 1 AND 64
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P5201',
                MESSAGE = 'validation_error: metadata tag is invalid';
        END IF;

        IF (
            SELECT count(*)
            FROM jsonb_array_elements_text(
                p_metadata -> 'tags'
            )
        ) <> (
            SELECT count(DISTINCT tag_value)
            FROM jsonb_array_elements_text(
                p_metadata -> 'tags'
            ) AS tag_value
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P5201',
                MESSAGE = 'validation_error: metadata tags must be unique';
        END IF;
    END IF;

    IF p_metadata ? 'project'
       AND (
           jsonb_typeof(p_metadata -> 'project') <> 'string'
           OR length(p_metadata ->> 'project') NOT BETWEEN 1 AND 128
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: metadata project is invalid';
    END IF;

    IF p_metadata ? 'conversation_ref'
       AND (
           jsonb_typeof(p_metadata -> 'conversation_ref') <> 'string'
           OR length(p_metadata ->> 'conversation_ref')
                NOT BETWEEN 1 AND 256
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: metadata conversation_ref is invalid';
    END IF;

    IF p_metadata ? 'source_title'
       AND (
           jsonb_typeof(p_metadata -> 'source_title') <> 'string'
           OR length(p_metadata ->> 'source_title')
                NOT BETWEEN 1 AND 256
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: metadata source_title is invalid';
    END IF;

    IF p_metadata ? 'language'
       AND (
           jsonb_typeof(p_metadata -> 'language') <> 'string'
           OR length(p_metadata ->> 'language') NOT BETWEEN 2 AND 16
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: metadata language is invalid';
    END IF;
END
$function$;

CREATE FUNCTION public.rag_api_validate_common(
    p_reason TEXT,
    p_source_ref TEXT,
    p_idempotency_key TEXT,
    p_request_hash TEXT,
    p_metadata JSONB
)
RETURNS VOID
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF p_reason IS NULL
       OR btrim(p_reason) = ''
       OR octet_length(p_reason) > 1024 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: reason is invalid';
    END IF;

    IF p_source_ref IS NOT NULL
       AND (
           btrim(p_source_ref) = ''
           OR octet_length(p_source_ref) > 2048
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: source_ref is invalid';
    END IF;

    IF p_idempotency_key IS NULL
       OR p_idempotency_key
            !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{7,254}$' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: idempotency_key is invalid';
    END IF;

    IF p_request_hash IS NULL
       OR p_request_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: request_hash is invalid';
    END IF;

    PERFORM public.rag_api_validate_metadata(p_metadata);
END
$function$;

CREATE FUNCTION public.rag_api_validate_revision_input(
    p_namespace TEXT,
    p_content TEXT,
    p_content_hash TEXT,
    p_source_type TEXT,
    p_source_ref TEXT,
    p_trust_level TEXT,
    p_reason TEXT,
    p_idempotency_key TEXT,
    p_request_hash TEXT,
    p_metadata JSONB
)
RETURNS VOID
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
    PERFORM public.rag_api_validate_common(
        p_reason,
        p_source_ref,
        p_idempotency_key,
        p_request_hash,
        p_metadata
    );

    IF p_namespace NOT IN (
        'personal_memory',
        'project_memory',
        'agent_observations',
        'quarantine'
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5208',
            MESSAGE = 'permission_denied: namespace is not writable';
    END IF;

    IF p_content IS NULL
       OR btrim(p_content) = ''
       OR octet_length(p_content) > 32768
       OR position(chr(13) IN p_content) > 0
       OR left(p_content, 1) = chr(65279) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: content is invalid';
    END IF;

    IF p_content_hash IS NULL
       OR p_content_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: content_hash is invalid';
    END IF;

    IF p_source_type NOT IN (
        'user_explicit',
        'user_implicit',
        'agent_inference',
        'external_document',
        'tool_output',
        'system_generated'
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: source_type is invalid';
    END IF;

    IF p_trust_level NOT IN (
        'untrusted',
        'low',
        'medium',
        'high'
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: trust_level is invalid';
    END IF;

    IF p_trust_level = 'high'
       AND p_source_type <> 'user_explicit' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: high trust requires user_explicit';
    END IF;

    IF p_namespace = 'agent_observations'
       AND p_trust_level = 'high' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: agent observations cannot be high trust';
    END IF;

    IF p_namespace = 'quarantine'
       AND p_trust_level <> 'untrusted' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: quarantine requires untrusted status';
    END IF;
END
$function$;

CREATE FUNCTION public.rag_api_memory_create(
    p_memory_id UUID,
    p_revision_id UUID,
    p_namespace TEXT,
    p_memory_type TEXT,
    p_content TEXT,
    p_content_hash TEXT,
    p_source_type TEXT,
    p_source_ref TEXT,
    p_trust_level TEXT,
    p_reason TEXT,
    p_idempotency_key TEXT,
    p_request_hash TEXT,
    p_metadata JSONB
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
    v_actor CONSTANT TEXT := 'openclaw-agent:main';
    v_status TEXT;
    v_event public.rag_memory_events%ROWTYPE;
    v_event_id BIGINT;
BEGIN
    IF p_memory_id IS NULL OR p_revision_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: generated identifiers are required';
    END IF;

    IF p_memory_type NOT IN (
        'fact',
        'preference',
        'decision',
        'plan',
        'constraint',
        'project_state',
        'procedure',
        'observation',
        'reference'
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: memory_type is invalid';
    END IF;

    PERFORM public.rag_api_validate_revision_input(
        p_namespace,
        p_content,
        p_content_hash,
        p_source_type,
        p_source_ref,
        p_trust_level,
        p_reason,
        p_idempotency_key,
        p_request_hash,
        p_metadata
    );

    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            'phase5.2:' || p_idempotency_key,
            520003
        )
    );

    SELECT event_record.*
    INTO v_event
    FROM public.rag_memory_events AS event_record
    WHERE event_record.idempotency_key = p_idempotency_key;

    IF FOUND THEN
        IF v_event.event_type <> 'create'
           OR v_event.new_state ->> 'request_hash'
                IS DISTINCT FROM p_request_hash THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P5207',
                MESSAGE = 'idempotency_conflict';
        END IF;

        RETURN QUERY
        SELECT
            'create'::TEXT,
            v_event.memory_id,
            v_event.revision_id,
            v_event.id,
            (v_event.new_state ->> 'current_version')::INTEGER,
            (v_event.new_state ->> 'state_version')::BIGINT,
            v_event.new_state ->> 'status',
            v_event.new_state ->> 'content_hash',
            TRUE;

        RETURN;
    END IF;

    v_status :=
        CASE
            WHEN p_source_type = 'user_explicit'
             AND p_trust_level = 'high'
             AND p_namespace <> 'quarantine'
            THEN 'active'
            ELSE 'candidate'
        END;

    INSERT INTO public.rag_memories (
        id,
        namespace,
        memory_type,
        status,
        current_version,
        current_revision_id,
        state_version,
        created_by,
        updated_by,
        metadata
    )
    VALUES (
        p_memory_id,
        p_namespace,
        p_memory_type,
        v_status,
        1,
        p_revision_id,
        1,
        v_actor,
        v_actor,
        p_metadata
    );

    INSERT INTO public.rag_memory_revisions (
        id,
        memory_id,
        version,
        content,
        content_hash,
        source_type,
        source_ref,
        trust_level,
        reason,
        created_by,
        supersedes_revision_id,
        embedding_status,
        metadata
    )
    VALUES (
        p_revision_id,
        p_memory_id,
        1,
        p_content,
        p_content_hash,
        p_source_type,
        p_source_ref,
        p_trust_level,
        p_reason,
        v_actor,
        NULL,
        'pending',
        p_metadata
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
        'create',
        v_actor,
        p_source_ref,
        p_reason,
        p_idempotency_key,
        NULL,
        jsonb_build_object(
            'operation', 'create',
            'request_hash', p_request_hash,
            'status', v_status,
            'current_version', 1,
            'state_version', 1,
            'revision_id', p_revision_id,
            'content_hash', p_content_hash
        )
    )
    RETURNING id INTO v_event_id;

    RETURN QUERY
    SELECT
        'create'::TEXT,
        p_memory_id,
        p_revision_id,
        v_event_id,
        1,
        1::BIGINT,
        v_status,
        p_content_hash,
        FALSE;
END
$function$;

CREATE FUNCTION public.rag_api_memory_update(
    p_memory_id UUID,
    p_revision_id UUID,
    p_expected_version INTEGER,
    p_expected_state_version BIGINT,
    p_content TEXT,
    p_content_hash TEXT,
    p_source_type TEXT,
    p_source_ref TEXT,
    p_trust_level TEXT,
    p_reason TEXT,
    p_idempotency_key TEXT,
    p_request_hash TEXT,
    p_metadata JSONB
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
    v_actor CONSTANT TEXT := 'openclaw-agent:main';
    v_memory public.rag_memories%ROWTYPE;
    v_event public.rag_memory_events%ROWTYPE;
    v_current_hash TEXT;
    v_event_id BIGINT;
    v_new_version INTEGER;
    v_new_state_version BIGINT;
BEGIN
    IF p_memory_id IS NULL
       OR p_revision_id IS NULL
       OR p_expected_version IS NULL
       OR p_expected_version < 1
       OR p_expected_state_version IS NULL
       OR p_expected_state_version < 1 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: identifiers or versions are invalid';
    END IF;

    PERFORM public.rag_api_validate_common(
        p_reason,
        p_source_ref,
        p_idempotency_key,
        p_request_hash,
        p_metadata
    );

    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            'phase5.2:' || p_idempotency_key,
            520003
        )
    );

    SELECT event_record.*
    INTO v_event
    FROM public.rag_memory_events AS event_record
    WHERE event_record.idempotency_key = p_idempotency_key;

    IF FOUND THEN
        IF v_event.event_type <> 'update'
           OR v_event.new_state ->> 'request_hash'
                IS DISTINCT FROM p_request_hash THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P5207',
                MESSAGE = 'idempotency_conflict';
        END IF;

        RETURN QUERY
        SELECT
            'update'::TEXT,
            v_event.memory_id,
            v_event.revision_id,
            v_event.id,
            (v_event.new_state ->> 'current_version')::INTEGER,
            (v_event.new_state ->> 'state_version')::BIGINT,
            v_event.new_state ->> 'status',
            v_event.new_state ->> 'content_hash',
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

    PERFORM public.rag_api_validate_revision_input(
        v_memory.namespace,
        p_content,
        p_content_hash,
        p_source_type,
        p_source_ref,
        p_trust_level,
        p_reason,
        p_idempotency_key,
        p_request_hash,
        p_metadata
    );

    IF v_memory.status = 'archived' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5204',
            MESSAGE = 'invalid_state: archived memory must be restored first';
    END IF;

    IF v_memory.status = 'rejected' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5208',
            MESSAGE = 'permission_denied';
    END IF;

    IF v_memory.current_version <> p_expected_version
       OR v_memory.state_version <> p_expected_state_version THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5203',
            MESSAGE = 'concurrency_conflict';
    END IF;

    SELECT revision_record.content_hash
    INTO v_current_hash
    FROM public.rag_memory_revisions AS revision_record
    WHERE revision_record.memory_id = v_memory.id
      AND revision_record.id = v_memory.current_revision_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5204',
            MESSAGE = 'invalid_state: current revision is missing';
    END IF;

    IF v_current_hash = p_content_hash THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: content is unchanged';
    END IF;

    v_new_version := v_memory.current_version + 1;
    v_new_state_version := v_memory.state_version + 1;

    INSERT INTO public.rag_memory_revisions (
        id,
        memory_id,
        version,
        content,
        content_hash,
        source_type,
        source_ref,
        trust_level,
        reason,
        created_by,
        supersedes_revision_id,
        embedding_status,
        metadata
    )
    VALUES (
        p_revision_id,
        p_memory_id,
        v_new_version,
        p_content,
        p_content_hash,
        p_source_type,
        p_source_ref,
        p_trust_level,
        p_reason,
        v_actor,
        v_memory.current_revision_id,
        'pending',
        p_metadata
    );

    UPDATE public.rag_memories
    SET
        current_version = v_new_version,
        current_revision_id = p_revision_id,
        state_version = v_new_state_version,
        updated_by = v_actor,
        updated_at = CURRENT_TIMESTAMP,
        metadata = p_metadata
    WHERE id = p_memory_id;

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
        'update',
        v_actor,
        p_source_ref,
        p_reason,
        p_idempotency_key,
        jsonb_build_object(
            'operation', 'update',
            'status', v_memory.status,
            'current_version', v_memory.current_version,
            'state_version', v_memory.state_version,
            'revision_id', v_memory.current_revision_id,
            'content_hash', v_current_hash
        ),
        jsonb_build_object(
            'operation', 'update',
            'request_hash', p_request_hash,
            'status', v_memory.status,
            'current_version', v_new_version,
            'state_version', v_new_state_version,
            'revision_id', p_revision_id,
            'content_hash', p_content_hash
        )
    )
    RETURNING id INTO v_event_id;

    RETURN QUERY
    SELECT
        'update'::TEXT,
        p_memory_id,
        p_revision_id,
        v_event_id,
        v_new_version,
        v_new_state_version,
        v_memory.status,
        p_content_hash,
        FALSE;
END
$function$;

CREATE FUNCTION public.rag_api_memory_archive(
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
    v_actor CONSTANT TEXT := 'openclaw-agent:main';
    v_memory public.rag_memories%ROWTYPE;
    v_event public.rag_memory_events%ROWTYPE;
    v_current_hash TEXT;
    v_event_id BIGINT;
    v_new_state_version BIGINT;
BEGIN
    IF p_memory_id IS NULL
       OR p_expected_version IS NULL
       OR p_expected_version < 1
       OR p_expected_state_version IS NULL
       OR p_expected_state_version < 1 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: identifiers or versions are invalid';
    END IF;

    PERFORM public.rag_api_validate_common(
        p_reason,
        p_source_ref,
        p_idempotency_key,
        p_request_hash,
        '{}'::JSONB
    );

    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            'phase5.2:' || p_idempotency_key,
            520003
        )
    );

    SELECT event_record.*
    INTO v_event
    FROM public.rag_memory_events AS event_record
    WHERE event_record.idempotency_key = p_idempotency_key;

    IF FOUND THEN
        IF v_event.event_type <> 'archive'
           OR v_event.new_state ->> 'request_hash'
                IS DISTINCT FROM p_request_hash THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P5207',
                MESSAGE = 'idempotency_conflict';
        END IF;

        RETURN QUERY
        SELECT
            'archive'::TEXT,
            v_event.memory_id,
            v_event.revision_id,
            v_event.id,
            (v_event.new_state ->> 'current_version')::INTEGER,
            (v_event.new_state ->> 'state_version')::BIGINT,
            v_event.new_state ->> 'status',
            v_event.new_state ->> 'content_hash',
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

    IF v_memory.status = 'archived' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5205',
            MESSAGE = 'already_archived';
    END IF;

    IF v_memory.status = 'rejected' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5208',
            MESSAGE = 'permission_denied';
    END IF;

    IF v_memory.current_version <> p_expected_version
       OR v_memory.state_version <> p_expected_state_version THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5203',
            MESSAGE = 'concurrency_conflict';
    END IF;

    SELECT revision_record.content_hash
    INTO v_current_hash
    FROM public.rag_memory_revisions AS revision_record
    WHERE revision_record.memory_id = v_memory.id
      AND revision_record.id = v_memory.current_revision_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5204',
            MESSAGE = 'invalid_state: current revision is missing';
    END IF;

    v_new_state_version := v_memory.state_version + 1;

    UPDATE public.rag_memories
    SET
        status = 'archived',
        state_version = v_new_state_version,
        archived_at = CURRENT_TIMESTAMP,
        archived_by = v_actor,
        archive_reason = p_reason,
        updated_by = v_actor,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = p_memory_id;

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
        v_memory.current_revision_id,
        'archive',
        v_actor,
        p_source_ref,
        p_reason,
        p_idempotency_key,
        jsonb_build_object(
            'operation', 'archive',
            'status', v_memory.status,
            'current_version', v_memory.current_version,
            'state_version', v_memory.state_version,
            'revision_id', v_memory.current_revision_id,
            'content_hash', v_current_hash
        ),
        jsonb_build_object(
            'operation', 'archive',
            'request_hash', p_request_hash,
            'status', 'archived',
            'current_version', v_memory.current_version,
            'state_version', v_new_state_version,
            'revision_id', v_memory.current_revision_id,
            'content_hash', v_current_hash
        )
    )
    RETURNING id INTO v_event_id;

    RETURN QUERY
    SELECT
        'archive'::TEXT,
        p_memory_id,
        v_memory.current_revision_id,
        v_event_id,
        v_memory.current_version,
        v_new_state_version,
        'archived'::TEXT,
        v_current_hash,
        FALSE;
END
$function$;

CREATE FUNCTION public.rag_api_memory_restore(
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
    v_actor CONSTANT TEXT := 'openclaw-agent:main';
    v_memory public.rag_memories%ROWTYPE;
    v_event public.rag_memory_events%ROWTYPE;
    v_current_hash TEXT;
    v_event_id BIGINT;
    v_new_state_version BIGINT;
BEGIN
    IF p_memory_id IS NULL
       OR p_expected_version IS NULL
       OR p_expected_version < 1
       OR p_expected_state_version IS NULL
       OR p_expected_state_version < 1 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5201',
            MESSAGE = 'validation_error: identifiers or versions are invalid';
    END IF;

    PERFORM public.rag_api_validate_common(
        p_reason,
        p_source_ref,
        p_idempotency_key,
        p_request_hash,
        '{}'::JSONB
    );

    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            'phase5.2:' || p_idempotency_key,
            520003
        )
    );

    SELECT event_record.*
    INTO v_event
    FROM public.rag_memory_events AS event_record
    WHERE event_record.idempotency_key = p_idempotency_key;

    IF FOUND THEN
        IF v_event.event_type <> 'restore'
           OR v_event.new_state ->> 'request_hash'
                IS DISTINCT FROM p_request_hash THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P5207',
                MESSAGE = 'idempotency_conflict';
        END IF;

        RETURN QUERY
        SELECT
            'restore'::TEXT,
            v_event.memory_id,
            v_event.revision_id,
            v_event.id,
            (v_event.new_state ->> 'current_version')::INTEGER,
            (v_event.new_state ->> 'state_version')::BIGINT,
            v_event.new_state ->> 'status',
            v_event.new_state ->> 'content_hash',
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

    IF v_memory.status = 'rejected' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5208',
            MESSAGE = 'permission_denied';
    END IF;

    IF v_memory.status <> 'archived' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5206',
            MESSAGE = 'not_archived';
    END IF;

    IF v_memory.current_version <> p_expected_version
       OR v_memory.state_version <> p_expected_state_version THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5203',
            MESSAGE = 'concurrency_conflict';
    END IF;

    SELECT revision_record.content_hash
    INTO v_current_hash
    FROM public.rag_memory_revisions AS revision_record
    WHERE revision_record.memory_id = v_memory.id
      AND revision_record.id = v_memory.current_revision_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P5204',
            MESSAGE = 'invalid_state: current revision is missing';
    END IF;

    v_new_state_version := v_memory.state_version + 1;

    UPDATE public.rag_memories
    SET
        status = 'active',
        state_version = v_new_state_version,
        archived_at = NULL,
        archived_by = NULL,
        archive_reason = NULL,
        updated_by = v_actor,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = p_memory_id;

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
        v_memory.current_revision_id,
        'restore',
        v_actor,
        p_source_ref,
        p_reason,
        p_idempotency_key,
        jsonb_build_object(
            'operation', 'restore',
            'status', v_memory.status,
            'current_version', v_memory.current_version,
            'state_version', v_memory.state_version,
            'revision_id', v_memory.current_revision_id,
            'content_hash', v_current_hash
        ),
        jsonb_build_object(
            'operation', 'restore',
            'request_hash', p_request_hash,
            'status', 'active',
            'current_version', v_memory.current_version,
            'state_version', v_new_state_version,
            'revision_id', v_memory.current_revision_id,
            'content_hash', v_current_hash
        )
    )
    RETURNING id INTO v_event_id;

    RETURN QUERY
    SELECT
        'restore'::TEXT,
        p_memory_id,
        v_memory.current_revision_id,
        v_event_id,
        v_memory.current_version,
        v_new_state_version,
        'active'::TEXT,
        v_current_hash,
        FALSE;
END
$function$;

ALTER FUNCTION public.rag_api_validate_metadata(JSONB)
    OWNER TO rag_memory_api_owner;

ALTER FUNCTION public.rag_api_validate_common(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    JSONB
)
    OWNER TO rag_memory_api_owner;

ALTER FUNCTION public.rag_api_validate_revision_input(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    JSONB
)
    OWNER TO rag_memory_api_owner;

ALTER FUNCTION public.rag_api_memory_create(
    UUID,
    UUID,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    JSONB
)
    OWNER TO rag_memory_api_owner;

ALTER FUNCTION public.rag_api_memory_update(
    UUID,
    UUID,
    INTEGER,
    BIGINT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    JSONB
)
    OWNER TO rag_memory_api_owner;

ALTER FUNCTION public.rag_api_memory_archive(
    UUID,
    INTEGER,
    BIGINT,
    TEXT,
    TEXT,
    TEXT,
    TEXT
)
    OWNER TO rag_memory_api_owner;

ALTER FUNCTION public.rag_api_memory_restore(
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
ON FUNCTION public.rag_api_validate_metadata(JSONB)
FROM PUBLIC;

REVOKE ALL
ON FUNCTION public.rag_api_validate_common(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    JSONB
)
FROM PUBLIC;

REVOKE ALL
ON FUNCTION public.rag_api_validate_revision_input(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    JSONB
)
FROM PUBLIC;

REVOKE ALL
ON FUNCTION public.rag_api_memory_create(
    UUID,
    UUID,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    JSONB
)
FROM PUBLIC;

REVOKE ALL
ON FUNCTION public.rag_api_memory_update(
    UUID,
    UUID,
    INTEGER,
    BIGINT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    JSONB
)
FROM PUBLIC;

REVOKE ALL
ON FUNCTION public.rag_api_memory_archive(
    UUID,
    INTEGER,
    BIGINT,
    TEXT,
    TEXT,
    TEXT,
    TEXT
)
FROM PUBLIC;

REVOKE ALL
ON FUNCTION public.rag_api_memory_restore(
    UUID,
    INTEGER,
    BIGINT,
    TEXT,
    TEXT,
    TEXT,
    TEXT
)
FROM PUBLIC;

GRANT EXECUTE
ON FUNCTION public.rag_api_memory_create(
    UUID,
    UUID,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    JSONB
)
TO rag_mcp_runtime;

GRANT EXECUTE
ON FUNCTION public.rag_api_memory_update(
    UUID,
    UUID,
    INTEGER,
    BIGINT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    JSONB
)
TO rag_mcp_runtime;

GRANT EXECUTE
ON FUNCTION public.rag_api_memory_archive(
    UUID,
    INTEGER,
    BIGINT,
    TEXT,
    TEXT,
    TEXT,
    TEXT
)
TO rag_mcp_runtime;

GRANT EXECUTE
ON FUNCTION public.rag_api_memory_restore(
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
    'phase5.2-0003',
    'Add governed SECURITY DEFINER memory mutation API'
);

COMMIT;
