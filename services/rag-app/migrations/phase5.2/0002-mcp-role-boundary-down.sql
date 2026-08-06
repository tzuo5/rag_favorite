\set ON_ERROR_STOP on

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

SELECT pg_advisory_xact_lock(74518, 520002);

DO $phase52$
DECLARE
    allow_destructive BOOLEAN :=
        COALESCE(
            current_setting(
                'rag.allow_destructive_rollback',
                true
            ),
            'off'
        ) = 'on';
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM public.rag_schema_migrations
        WHERE version = 'phase5.2-0002'
    ) THEN
        RAISE EXCEPTION
            'Migration phase5.2-0002 is not recorded';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'rag_memories'
          AND column_name = 'state_version'
    ) THEN
        RAISE EXCEPTION
            'rag_memories.state_version is missing';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = 'rag_memory_api_owner'
    ) OR NOT EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = 'rag_mcp_runtime'
    ) THEN
        RAISE EXCEPTION
            'Phase 5.2 database roles are incomplete';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_proc AS procedure
        JOIN pg_namespace AS namespace
          ON namespace.oid = procedure.pronamespace
        WHERE namespace.nspname = 'public'
          AND procedure.proname LIKE 'rag_api_memory_%'
    ) THEN
        RAISE EXCEPTION
            'Phase 5.2 API routines still exist; roll them back first';
    END IF;

    IF NOT allow_destructive
       AND EXISTS (
           SELECT 1
           FROM public.rag_memories
       ) THEN
        RAISE EXCEPTION
            'Governed memories exist; rollback would discard state_version history. Explicitly set rag.allow_destructive_rollback=on';
    END IF;
END
$phase52$;

REVOKE ALL PRIVILEGES
ON SEQUENCE public.rag_memory_events_id_seq
FROM rag_memory_api_owner;

REVOKE ALL PRIVILEGES
ON TABLE
    public.rag_memories,
    public.rag_memory_revisions,
    public.rag_memory_events
FROM rag_memory_api_owner;

REVOKE USAGE
ON SCHEMA public
FROM rag_mcp_runtime;

REVOKE USAGE
ON SCHEMA public
FROM rag_memory_api_owner;

DROP ROLE rag_mcp_runtime;
DROP ROLE rag_memory_api_owner;

ALTER TABLE public.rag_memories
    DROP CONSTRAINT rag_memories_state_version_positive;

ALTER TABLE public.rag_memories
    DROP COLUMN state_version;

DELETE FROM public.rag_schema_migrations
WHERE version = 'phase5.2-0002';

COMMIT;
