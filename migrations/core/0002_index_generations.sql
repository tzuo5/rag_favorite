-- The original public document index remains untouched as a legacy snapshot.
-- Each verified generation owns an isolated document/chunk schema.
CREATE TABLE IF NOT EXISTS public.rag_index_generations (
    id text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    schema_name text NOT NULL UNIQUE CHECK (schema_name ~ '^rag_gen_[a-f0-9]{24}$'),
    space_id text NOT NULL,
    embedding_settings jsonb NOT NULL,
    collection_roots jsonb NOT NULL,
    state text NOT NULL DEFAULT 'shadow' CHECK (state IN ('shadow', 'ready')),
    manifest_sha256 text,
    document_count bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    ready_at timestamptz
);

CREATE TABLE IF NOT EXISTS public.rag_index_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    active_generation text NOT NULL REFERENCES public.rag_index_generations(id),
    activated_at timestamptz NOT NULL DEFAULT now()
);
