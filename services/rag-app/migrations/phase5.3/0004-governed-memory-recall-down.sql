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
        WHERE version = 'phase5.3-0004'
    ) THEN
        RAISE EXCEPTION
            'Migration phase5.3-0004 is not recorded';
    END IF;
END
$phase53$;

REVOKE EXECUTE
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
FROM rag_mcp_runtime;

REVOKE EXECUTE
ON FUNCTION public.rag_api_memory_search(VECTOR, INTEGER)
FROM rag_mcp_runtime;

REVOKE EXECUTE
ON FUNCTION public.rag_api_memory_restore_ready(
    UUID,
    INTEGER,
    BIGINT,
    TEXT,
    TEXT,
    TEXT,
    TEXT
)
FROM rag_mcp_runtime;

DROP FUNCTION public.rag_api_memory_restore_ready(
    UUID,
    INTEGER,
    BIGINT,
    TEXT,
    TEXT,
    TEXT,
    TEXT
);

DROP FUNCTION public.rag_api_memory_search(VECTOR, INTEGER);

DROP FUNCTION public.rag_api_memory_embedding_succeed(
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
);

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

REVOKE UPDATE (
    embedding_status,
    embedding_model,
    embedding_dimensions,
    embedding
)
ON TABLE public.rag_memory_revisions
FROM rag_memory_api_owner;

CREATE OR REPLACE FUNCTION public.rag_reject_immutable_change()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
    RAISE EXCEPTION
        '% is immutable; % is not permitted',
        TG_TABLE_NAME,
        TG_OP;
END
$function$;

DELETE FROM public.rag_schema_migrations
WHERE version = 'phase5.3-0004';

COMMIT;
