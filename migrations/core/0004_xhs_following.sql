-- Owner-bound discovery, independent of the media processing queue.
CREATE TABLE IF NOT EXISTS public.rag_xhs_accounts (
    library_id text PRIMARY KEY,
    owner_hash text NOT NULL,
    paused boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.rag_xhs_authors (
    library_id text NOT NULL REFERENCES public.rag_xhs_accounts(library_id),
    author_id text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    nickname text NOT NULL DEFAULT '',
    first_seen timestamptz NOT NULL DEFAULT now(),
    last_seen timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(library_id, author_id)
);
CREATE TABLE IF NOT EXISTS public.rag_xhs_scans (
    id uuid PRIMARY KEY,
    library_id text NOT NULL REFERENCES public.rag_xhs_accounts(library_id),
    collection text NOT NULL,
    mode text NOT NULL CHECK(mode IN ('history','incremental','full')),
    state text NOT NULL CHECK(state IN ('running','partial','paused','backpressure','blocked','complete')),
    following_complete boolean NOT NULL DEFAULT false,
    following_count integer,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rag_xhs_scan_latest ON public.rag_xhs_scans(library_id, collection, created_at DESC);
CREATE TABLE IF NOT EXISTS public.rag_xhs_author_scans (
    scan_id uuid NOT NULL REFERENCES public.rag_xhs_scans(id) ON DELETE CASCADE,
    author_id text NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','complete','blocked','cancelled')),
    checkpoint jsonb NOT NULL DEFAULT '{"cursor":"","seen_cursors":[],"pages":0,"known_pages":0}',
    error_code text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(scan_id, author_id)
);
CREATE TABLE IF NOT EXISTS public.rag_xhs_sources (
    library_id text NOT NULL,
    collection text NOT NULL,
    note_id text NOT NULL,
    origin text NOT NULL CHECK(origin IN ('manual','favorites','following','live_photo')),
    author_id text NOT NULL DEFAULT '',
    component text NOT NULL DEFAULT '',
    job_id uuid NOT NULL REFERENCES public.rag_video_jobs(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(library_id, collection, note_id, origin, author_id, component)
);
CREATE INDEX IF NOT EXISTS rag_xhs_sources_job ON public.rag_xhs_sources(job_id);
