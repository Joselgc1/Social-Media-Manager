-- Add discovered Story lifecycle and private Instagram conversation context.
BEGIN;

ALTER TABLE instagram_content
    ADD COLUMN IF NOT EXISTS thumbnail_url TEXT,
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

ALTER TABLE conversation_sessions
    ADD COLUMN IF NOT EXISTS instagram_content_context JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS instagram_context_expires_at TIMESTAMPTZ;

ALTER TABLE kommo_message_jobs
    ADD COLUMN IF NOT EXISTS instagram_content_context JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_meta_story_reply_correlation
    ON meta_instagram_context_events(correlation_status, normalized_text_hash, event_timestamp)
    WHERE event_type = 'story_reply' AND matched_kommo_job_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_instagram_content_story_media
    ON instagram_content(media_id)
    WHERE content_type = 'story' AND media_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_conversation_sessions_instagram_context_expiry
    ON conversation_sessions(instagram_context_expires_at)
    WHERE instagram_context_expires_at IS NOT NULL;

COMMENT ON COLUMN kommo_message_jobs.instagram_content_context IS
    'Private Instagram content context; never applies public-comment restrictions.';

INSERT INTO schema_migrations (version, name) VALUES
    (6, 'instagram_story_context')
ON CONFLICT (version) DO NOTHING;

COMMIT;
