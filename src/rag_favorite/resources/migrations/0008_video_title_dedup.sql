-- Title identity is independent of topic metadata. Originals remain addressable.
ALTER TABLE public.rag_video_jobs ADD COLUMN title_key text NOT NULL DEFAULT '';
ALTER TABLE public.rag_knowledge_documents ADD COLUMN title_key text NOT NULL DEFAULT '';
CREATE INDEX rag_video_title_identity ON public.rag_video_jobs(library_id,title_key) WHERE title_key<>'';
CREATE INDEX rag_document_title_identity ON public.rag_knowledge_documents(library_id,title_key) WHERE title_key<>'';
ALTER TABLE public.rag_video_jobs DROP CONSTRAINT rag_video_jobs_state_check;
ALTER TABLE public.rag_video_jobs ADD CONSTRAINT rag_video_jobs_state_check
 CHECK(state IN ('queued','running','published','complete','failed','blocked','duplicate'));
CREATE TABLE public.rag_title_job_aliases (
 duplicate_job_id uuid PRIMARY KEY REFERENCES public.rag_video_jobs(id) ON DELETE CASCADE,
 library_id text NOT NULL,
 canonical_job_id uuid NOT NULL REFERENCES public.rag_video_jobs(id) ON DELETE CASCADE,
 title_key text NOT NULL,
 previous_state text NOT NULL, previous_stage text NOT NULL, previous_payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 CHECK(duplicate_job_id<>canonical_job_id)
);
