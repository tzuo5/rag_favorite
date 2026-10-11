-- Derived video knowledge is independent of text index generations.
CREATE TABLE IF NOT EXISTS public.rag_video_jobs (
    id uuid PRIMARY KEY,
    library_id text NOT NULL,
    collection text NOT NULL,
    state text NOT NULL CHECK (state IN ('queued','running','published','complete','failed','blocked')),
    stage text NOT NULL DEFAULT 'queued',
    payload jsonb NOT NULL,
    error_code text,
    priority integer NOT NULL DEFAULT 10,
    attempts integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rag_video_job_queue ON public.rag_video_jobs(library_id,state,priority,created_at);
CREATE TABLE IF NOT EXISTS public.rag_videos (
    id uuid PRIMARY KEY REFERENCES public.rag_video_jobs(id),
    library_id text NOT NULL,
    collection text NOT NULL,
    title text NOT NULL,
    source_label text NOT NULL,
    content_sha256 text NOT NULL,
    classification jsonb NOT NULL,
    transcript jsonb NOT NULL,
    provider_usage jsonb NOT NULL,
    source_deleted boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.rag_video_segments (
    id text PRIMARY KEY,
    video_id uuid NOT NULL REFERENCES public.rag_videos(id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    start_seconds double precision,
    end_seconds double precision,
    timing_precision text NOT NULL,
    transcript text NOT NULL,
    caption text NOT NULL,
    facts jsonb NOT NULL,
    evidence jsonb NOT NULL,
    text_space_id text NOT NULL,
    text_embedding vector NOT NULL,
    video_space_id text,
    video_embedding vector(1024),
    UNIQUE(video_id,ordinal),
    CHECK ((start_seconds IS NULL AND end_seconds IS NULL) OR (start_seconds >= 0 AND end_seconds >= start_seconds)),
    CHECK ((video_embedding IS NULL) = (video_space_id IS NULL))
);
CREATE TABLE IF NOT EXISTS public.rag_video_edges (
    segment_id text NOT NULL REFERENCES public.rag_video_segments(id) ON DELETE CASCADE,
    subject text NOT NULL,
    relation text NOT NULL,
    object text NOT NULL,
    provenance jsonb NOT NULL,
    PRIMARY KEY(segment_id,subject,relation,object)
);
