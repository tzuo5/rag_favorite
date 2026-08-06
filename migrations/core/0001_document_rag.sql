CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS rag_documents (
    id bigserial PRIMARY KEY,
    source_path text NOT NULL UNIQUE,
    knowledge_base text NOT NULL,
    source_relative_path text NOT NULL,
    source_name text NOT NULL,
    source_type text NOT NULL,
    source_sha256 text NOT NULL,
    embedding_model text NOT NULL,
    embedding_dimensions integer NOT NULL,
    chunking_version integer NOT NULL DEFAULT 1,
    index_version integer NOT NULL DEFAULT 3,
    indexed_at timestamptz NOT NULL DEFAULT now()
);

-- The default Phase 1 model uses 1024 dimensions. A later migration runner
-- will generate a guarded dimension migration when the configured model
-- changes; changing vector dimensions is not safe as an in-place ALTER.
CREATE TABLE IF NOT EXISTS rag_chunks (
    id bigserial PRIMARY KEY,
    document_id bigint NOT NULL
        REFERENCES rag_documents(id)
        ON DELETE CASCADE,
    chunk_index integer NOT NULL,
    content text NOT NULL,
    character_count integer NOT NULL,
    embedding vector(1024) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS rag_chunks_document_id_idx
    ON rag_chunks(document_id);

CREATE INDEX IF NOT EXISTS rag_documents_knowledge_base_idx
    ON rag_documents(knowledge_base);
