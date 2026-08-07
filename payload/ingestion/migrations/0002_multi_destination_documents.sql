BEGIN;

ALTER TABLE video_knowledge_documents
    ADD COLUMN IF NOT EXISTS record_id bigserial;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='video_knowledge_documents'::regclass
          AND conname='video_knowledge_documents_pkey'
          AND pg_get_constraintdef(oid)='PRIMARY KEY (id)'
    ) THEN
        ALTER TABLE video_knowledge_documents DROP CONSTRAINT video_knowledge_documents_pkey;
    END IF;
END $$;

ALTER TABLE video_knowledge_documents
    ALTER COLUMN record_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='video_knowledge_documents'::regclass
          AND conname='video_knowledge_documents_pkey'
    ) THEN
        ALTER TABLE video_knowledge_documents
            ADD CONSTRAINT video_knowledge_documents_pkey PRIMARY KEY (record_id);
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS video_documents_id_destination_key
    ON video_knowledge_documents (id, selected_knowledge_base);

COMMIT;
