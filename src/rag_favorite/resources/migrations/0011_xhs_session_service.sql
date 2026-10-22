ALTER TABLE public.rag_video_pipeline_control
    ADD COLUMN xhs_session_state jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN xhs_session_version bigint NOT NULL DEFAULT 0,
    ADD COLUMN xhs_download_blocked boolean NOT NULL DEFAULT false;

-- Legacy authentication flags are evidence to recheck, not confirmed logouts.
UPDATE public.rag_video_pipeline_control
SET xhs_session_state = jsonb_build_object('paths', jsonb_build_object('note',
        jsonb_build_object('state', 'unknown', 'code', 'XHS_SESSION_UNKNOWN',
                          'retry_at', extract(epoch FROM now()), 'legacy_pause', true))),
    xhs_login_required = false
WHERE xhs_login_required;
