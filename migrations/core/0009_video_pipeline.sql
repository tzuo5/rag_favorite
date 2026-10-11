CREATE TABLE IF NOT EXISTS public.rag_video_pipeline (
    job_id uuid NOT NULL REFERENCES public.rag_video_jobs(id) ON DELETE CASCADE,
    library_id text NOT NULL,
    stage text NOT NULL CHECK (stage IN ('download','prepare','llm','publish')),
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','blocked')),
    owner text,
    token bigint NOT NULL DEFAULT 0,
    lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    retry_at timestamptz NOT NULL DEFAULT now(),
    error_code text,
    started_at timestamptz,
    completed_at timestamptz,
    elapsed_seconds double precision NOT NULL DEFAULT 0,
    artifact_ref text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(job_id,stage)
);
CREATE INDEX IF NOT EXISTS rag_pipeline_claim ON public.rag_video_pipeline(library_id,stage,status,retry_at);
CREATE TABLE IF NOT EXISTS public.rag_video_pipeline_control (
    library_id text PRIMARY KEY,
    consecutive_failures integer NOT NULL DEFAULT 0,
    circuit_until timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);
