\set ON_ERROR_STOP on

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

SELECT pg_advisory_xact_lock(74518, 520002);

DO $phase52$
BEGIN
    IF to_regclass('public.rag_memories') IS NULL
       OR to_regclass('public.rag_memory_revisions') IS NULL
       OR to_regclass('public.rag_memory_events') IS NULL
       OR to_regclass('public.rag_schema_migrations') IS NULL THEN
        RAISE EXCEPTION
            'Phase 5.1 governed-memory schema is incomplete';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM public.rag_schema_migrations
        WHERE version = 'phase5.1-0001'
    ) THEN
        RAISE EXCEPTION
            'Required migration phase5.1-0001 is not recorded';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.rag_schema_migrations
        WHERE version = 'phase5.2-0002'
    ) THEN
        RAISE EXCEPTION
            'Migration phase5.2-0002 is already recorded';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'rag_memories'
          AND column_name = 'state_version'
    ) THEN
        RAISE EXCEPTION
            'rag_memories.state_version already exists unexpectedly';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname IN (
            'rag_memory_api_owner',
            'rag_mcp_runtime'
        )
    ) THEN
        RAISE EXCEPTION
            'Phase 5.2 database roles already exist unexpectedly';
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
            'Phase 5.2 API routines already exist unexpectedly';
    END IF;
END
$phase52$;

ALTER TABLE public.rag_memories
    ADD COLUMN state_version BIGINT NOT NULL DEFAULT 1;

ALTER TABLE public.rag_memories
    ADD CONSTRAINT rag_memories_state_version_positive
    CHECK (state_version >= 1);

COMMENT ON COLUMN public.rag_memories.state_version IS
    'Optimistic-concurrency token for content and lifecycle mutations.';

CREATE ROLE rag_memory_api_owner
WITH
    NOLOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS;

CREATE ROLE rag_mcp_runtime
WITH
    NOLOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS
    CONNECTION LIMIT 5;

ALTER ROLE rag_mcp_runtime
    SET statement_timeout = '10s';

ALTER ROLE rag_mcp_runtime
    SET lock_timeout = '3s';

ALTER ROLE rag_mcp_runtime
    SET idle_in_transaction_session_timeout = '10s';

ALTER ROLE rag_mcp_runtime
    SET search_path = pg_catalog, public;

GRANT USAGE
ON SCHEMA public
TO rag_memory_api_owner;

GRANT USAGE
ON SCHEMA public
TO rag_mcp_runtime;

GRANT SELECT, INSERT, UPDATE
ON TABLE public.rag_memories
TO rag_memory_api_owner;

GRANT SELECT, INSERT
ON TABLE public.rag_memory_revisions
TO rag_memory_api_owner;

GRANT SELECT, INSERT
ON TABLE public.rag_memory_events
TO rag_memory_api_owner;

GRANT USAGE
ON SEQUENCE public.rag_memory_events_id_seq
TO rag_memory_api_owner;

REVOKE ALL PRIVILEGES
ON TABLE
    public.rag_memories,
    public.rag_memory_revisions,
    public.rag_memory_events
FROM rag_mcp_runtime;

REVOKE ALL PRIVILEGES
ON SEQUENCE public.rag_memory_events_id_seq
FROM rag_mcp_runtime;

INSERT INTO public.rag_schema_migrations (
    version,
    description
)
VALUES (
    'phase5.2-0002',
    'Add lifecycle concurrency token and staged least-privilege MCP roles'
);

COMMIT;
