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
        WHERE version = 'phase5.2-0003'
    ) THEN
        RAISE EXCEPTION
            'Migration phase5.2-0003 is not recorded';
    END IF;

    IF to_regprocedure(
        'public.rag_api_memory_create(uuid,uuid,text,text,text,text,text,text,text,text,text,text,jsonb)'
    ) IS NULL
       OR to_regprocedure(
        'public.rag_api_memory_update(uuid,uuid,integer,bigint,text,text,text,text,text,text,text,text,jsonb)'
       ) IS NULL
       OR to_regprocedure(
        'public.rag_api_memory_archive(uuid,integer,bigint,text,text,text,text)'
       ) IS NULL
       OR to_regprocedure(
        'public.rag_api_memory_restore(uuid,integer,bigint,text,text,text,text)'
       ) IS NULL THEN
        RAISE EXCEPTION
            'Phase 5.2 mutation API routines are incomplete';
    END IF;
END
$phase52$;

REVOKE EXECUTE
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
FROM rag_mcp_runtime;

REVOKE EXECUTE
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
FROM rag_mcp_runtime;

REVOKE EXECUTE
ON FUNCTION public.rag_api_memory_archive(
    UUID,
    INTEGER,
    BIGINT,
    TEXT,
    TEXT,
    TEXT,
    TEXT
)
FROM rag_mcp_runtime;

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

DROP FUNCTION public.rag_api_memory_restore(
    UUID,
    INTEGER,
    BIGINT,
    TEXT,
    TEXT,
    TEXT,
    TEXT
);

DROP FUNCTION public.rag_api_memory_archive(
    UUID,
    INTEGER,
    BIGINT,
    TEXT,
    TEXT,
    TEXT,
    TEXT
);

DROP FUNCTION public.rag_api_memory_update(
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
);

DROP FUNCTION public.rag_api_memory_create(
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
);

DROP FUNCTION public.rag_api_validate_revision_input(
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
);

DROP FUNCTION public.rag_api_validate_common(
    TEXT,
    TEXT,
    TEXT,
    TEXT,
    JSONB
);

DROP FUNCTION public.rag_api_validate_metadata(JSONB);

DELETE FROM public.rag_schema_migrations
WHERE version = 'phase5.2-0003';

COMMIT;
