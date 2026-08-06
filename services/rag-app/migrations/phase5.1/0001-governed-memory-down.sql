\set ON_ERROR_STOP on

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

SELECT pg_advisory_xact_lock(74518, 510001);

DO $$
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
    IF to_regclass('public.rag_memories') IS NULL
       OR to_regclass('public.rag_memory_revisions') IS NULL
       OR to_regclass('public.rag_memory_events') IS NULL THEN
        RAISE EXCEPTION
            'Phase 5.1 governed-memory tables are incomplete or missing';
    END IF;

    IF NOT allow_destructive
       AND (
            EXISTS (SELECT 1 FROM public.rag_memories)
            OR EXISTS (SELECT 1 FROM public.rag_memory_revisions)
            OR EXISTS (SELECT 1 FROM public.rag_memory_events)
       ) THEN
        RAISE EXCEPTION
            'Governed-memory data exists; rollback refused. An administrator must explicitly set rag.allow_destructive_rollback=on';
    END IF;
END
$$;

ALTER TABLE public.rag_memories
    DROP CONSTRAINT IF EXISTS rag_memories_current_revision_fk;

DROP TRIGGER IF EXISTS rag_memory_events_reject_delete
    ON public.rag_memory_events;

DROP TRIGGER IF EXISTS rag_memory_events_reject_update
    ON public.rag_memory_events;

DROP TRIGGER IF EXISTS rag_memory_revisions_reject_delete
    ON public.rag_memory_revisions;

DROP TRIGGER IF EXISTS rag_memory_revisions_reject_update
    ON public.rag_memory_revisions;

DROP TRIGGER IF EXISTS rag_memory_revisions_validate_chain
    ON public.rag_memory_revisions;

DROP TABLE public.rag_memory_events;
DROP TABLE public.rag_memory_revisions;
DROP TABLE public.rag_memories;

DROP FUNCTION IF EXISTS public.rag_reject_immutable_change();
DROP FUNCTION IF EXISTS public.rag_validate_revision_chain();

DELETE FROM public.rag_schema_migrations
WHERE version = 'phase5.1-0001';

COMMIT;
