-- Additive unified library; old source jobs, drafts and summary tables survive rollback.
CREATE TABLE public.rag_library_generations (
 library_id text NOT NULL, generation text NOT NULL,
 embedding_space_id text NOT NULL, state text NOT NULL DEFAULT 'building'
 CHECK(state IN ('building','ready','active')),
 manifest_sha256 text, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(library_id,generation)
);
CREATE TABLE public.rag_library_state (
 library_id text PRIMARY KEY, active_generation text NOT NULL,
 FOREIGN KEY(library_id,active_generation) REFERENCES public.rag_library_generations
);
CREATE TABLE public.rag_knowledge_documents (
 id uuid PRIMARY KEY, library_id text NOT NULL, source_kind text NOT NULL,
 title text NOT NULL, source_label text NOT NULL, draft text NOT NULL,
 draft_sha256 text NOT NULL, metadata jsonb NOT NULL,
 source_job_id uuid REFERENCES public.rag_video_jobs(id),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rag_knowledge_library ON public.rag_knowledge_documents(library_id);
CREATE TABLE public.rag_document_summaries (
 library_id text NOT NULL, generation text NOT NULL,
 document_id uuid NOT NULL REFERENCES public.rag_knowledge_documents(id),
 version text NOT NULL, summary text NOT NULL, search_text text NOT NULL, draft text NOT NULL,
 summary_sha256 text NOT NULL, draft_sha256 text NOT NULL,
 embedding_space_id text NOT NULL, published_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(library_id,generation,document_id),
 FOREIGN KEY(library_id,generation) REFERENCES public.rag_library_generations
);
CREATE TABLE public.rag_document_summary_chunks (
 library_id text NOT NULL, generation text NOT NULL, document_id uuid NOT NULL,
 ordinal integer NOT NULL, content text NOT NULL, embedding vector NOT NULL,
 PRIMARY KEY(library_id,generation,document_id,ordinal),
 FOREIGN KEY(library_id,generation,document_id) REFERENCES public.rag_document_summaries ON DELETE CASCADE
);
CREATE TABLE public.rag_document_summary_jobs (
 library_id text NOT NULL, generation text NOT NULL, document_id uuid NOT NULL
 REFERENCES public.rag_knowledge_documents(id), state text NOT NULL DEFAULT 'queued'
 CHECK(state IN ('queued','running','complete','failed','unsearchable')),
 attempts integer NOT NULL DEFAULT 0, error_code text,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(library_id,generation,document_id)
);
CREATE TABLE public.rag_legacy_reprocess (
 library_id text NOT NULL, migration_id text NOT NULL, source_key text NOT NULL,
 source_url text NOT NULL, job_id uuid REFERENCES public.rag_video_jobs(id),
 document_ids jsonb NOT NULL, PRIMARY KEY(library_id,migration_id,source_key)
);
