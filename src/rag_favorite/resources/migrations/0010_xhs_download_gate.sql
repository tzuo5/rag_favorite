ALTER TABLE public.rag_video_pipeline_control
    ADD COLUMN IF NOT EXISTS xhs_failures integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS xhs_circuit_until timestamptz,
    ADD COLUMN IF NOT EXISTS xhs_login_required boolean NOT NULL DEFAULT false;
