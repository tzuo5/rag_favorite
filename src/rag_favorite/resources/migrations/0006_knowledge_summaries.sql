-- Only validated summaries are searchable; drafts remain versioned read context.
CREATE TABLE IF NOT EXISTS public.rag_knowledge_summaries (
    job_id uuid PRIMARY KEY REFERENCES public.rag_video_jobs(id) ON DELETE CASCADE,
    library_id text NOT NULL,
    collection text NOT NULL,
    title text NOT NULL,
    source_label text NOT NULL,
    version text NOT NULL,
    draft_sha256 text NOT NULL,
    summary_sha256 text NOT NULL,
    summary text NOT NULL,
    draft text NOT NULL,
    embedding_space_id text NOT NULL,
    metadata jsonb NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rag_summary_scope
    ON public.rag_knowledge_summaries(library_id,collection,embedding_space_id);
CREATE TABLE IF NOT EXISTS public.rag_summary_chunks (
    job_id uuid NOT NULL REFERENCES public.rag_knowledge_summaries(job_id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    content text NOT NULL,
    embedding vector NOT NULL,
    PRIMARY KEY(job_id,ordinal)
);
CREATE TABLE IF NOT EXISTS public.rag_summary_jobs (
    job_id uuid PRIMARY KEY REFERENCES public.rag_video_jobs(id) ON DELETE CASCADE,
    library_id text NOT NULL,
    state text NOT NULL CHECK (state IN ('queued','running','complete','failed')),
    stage text NOT NULL DEFAULT 'queued',
    attempts integer NOT NULL DEFAULT 0,
    error_code text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rag_summary_queue ON public.rag_summary_jobs(library_id,state,updated_at);
-- New draft segments need no text vectors. Existing vectors remain recoverable.
ALTER TABLE public.rag_video_segments ALTER COLUMN text_embedding DROP NOT NULL;
ALTER TABLE public.rag_video_segments ALTER COLUMN text_space_id DROP NOT NULL;
