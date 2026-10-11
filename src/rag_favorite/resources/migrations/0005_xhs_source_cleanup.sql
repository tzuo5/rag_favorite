-- Provenance belongs to its job; deleting isolated/test jobs must remove associations.
ALTER TABLE public.rag_xhs_sources DROP CONSTRAINT IF EXISTS rag_xhs_sources_job_id_fkey;
ALTER TABLE public.rag_xhs_sources ADD CONSTRAINT rag_xhs_sources_job_id_fkey
    FOREIGN KEY(job_id) REFERENCES public.rag_video_jobs(id) ON DELETE CASCADE;
