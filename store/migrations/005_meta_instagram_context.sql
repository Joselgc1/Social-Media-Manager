-- Add context-only Meta Instagram events and Kommo correlation state.
BEGIN;

CREATE TABLE IF NOT EXISTS meta_instagram_context_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    instagram_account_id TEXT,
    sender_id TEXT,
    sender_username TEXT,
    message_text TEXT,
    normalized_text_hash TEXT NOT NULL,
    event_timestamp TIMESTAMPTZ NOT NULL,
    comment_id TEXT,
    parent_comment_id TEXT,
    message_id TEXT,
    media_id TEXT,
    media_type TEXT,
    media_product_type TEXT,
    media_timestamp TIMESTAMPTZ,
    media_thumbnail_url TEXT,
    story_id TEXT,
    story_url TEXT,
    media_permalink TEXT,
    media_caption TEXT,
    correlation_status TEXT NOT NULL DEFAULT 'pending',
    matched_kommo_job_id UUID REFERENCES kommo_message_jobs(id) ON DELETE SET NULL,
    correlation_details JSONB NOT NULL DEFAULT '{}'::jsonb,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE meta_instagram_context_events
    DROP CONSTRAINT IF EXISTS meta_instagram_context_events_event_type_check;
ALTER TABLE meta_instagram_context_events
    ADD CONSTRAINT meta_instagram_context_events_event_type_check CHECK (
        event_type IN ('comment', 'story_reply', 'story_mention')
    );

ALTER TABLE meta_instagram_context_events
    DROP CONSTRAINT IF EXISTS meta_instagram_context_events_correlation_status_check;
ALTER TABLE meta_instagram_context_events
    ADD CONSTRAINT meta_instagram_context_events_correlation_status_check CHECK (
        correlation_status IN ('pending', 'matched', 'ambiguous', 'expired')
    );

CREATE UNIQUE INDEX IF NOT EXISTS uq_meta_instagram_context_external_event
    ON meta_instagram_context_events(external_event_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_meta_instagram_context_comment
    ON meta_instagram_context_events(comment_id)
    WHERE comment_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_meta_instagram_context_message
    ON meta_instagram_context_events(message_id)
    WHERE message_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_meta_instagram_context_matched_job
    ON meta_instagram_context_events(matched_kommo_job_id)
    WHERE matched_kommo_job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_meta_instagram_context_correlation
    ON meta_instagram_context_events(correlation_status, expires_at, event_timestamp);
CREATE INDEX IF NOT EXISTS idx_meta_instagram_context_text_time
    ON meta_instagram_context_events(normalized_text_hash, event_timestamp)
    WHERE event_type = 'comment';
CREATE INDEX IF NOT EXISTS idx_meta_instagram_context_media
    ON meta_instagram_context_events(media_id)
    WHERE media_id IS NOT NULL;

DROP TRIGGER IF EXISTS trg_meta_instagram_context_updated_at ON meta_instagram_context_events;
CREATE TRIGGER trg_meta_instagram_context_updated_at
    BEFORE UPDATE ON meta_instagram_context_events
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE meta_instagram_context_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL PRIVILEGES ON meta_instagram_context_events FROM PUBLIC;

ALTER TABLE kommo_message_jobs
    ADD COLUMN IF NOT EXISTS meta_context_event_id UUID REFERENCES meta_instagram_context_events(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS context_status TEXT NOT NULL DEFAULT 'not_required',
    ADD COLUMN IF NOT EXISTS context_deadline_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS context_correlation_score INTEGER;

ALTER TABLE kommo_message_jobs
    DROP CONSTRAINT IF EXISTS kommo_message_jobs_context_status_check;
ALTER TABLE kommo_message_jobs
    ADD CONSTRAINT kommo_message_jobs_context_status_check CHECK (
        context_status IN ('not_required', 'pending', 'matched', 'ambiguous', 'timed_out')
    );

ALTER TABLE kommo_message_jobs
    DROP CONSTRAINT IF EXISTS kommo_message_jobs_status_check;
ALTER TABLE kommo_message_jobs
    ADD CONSTRAINT kommo_message_jobs_status_check CHECK (
        status IN (
            'pending',
            'prepared',
            'waiting_for_salesbot',
            'waiting_for_context',
            'ready',
            'processing',
            'continuing',
            'sent',
            'discarded',
            'delivery_unknown',
            'failed'
        )
    );

DROP INDEX IF EXISTS uq_kommo_message_jobs_active_salesbot;
CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_message_jobs_active_salesbot
    ON kommo_message_jobs(correlation_id)
    WHERE status IN (
        'prepared', 'waiting_for_salesbot', 'waiting_for_context', 'ready', 'processing', 'continuing'
    );
CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_message_jobs_meta_context_event
    ON kommo_message_jobs(meta_context_event_id)
    WHERE meta_context_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kommo_message_jobs_context_due
    ON kommo_message_jobs(status, context_deadline_at, created_at)
    WHERE status = 'waiting_for_context';

COMMENT ON TABLE meta_instagram_context_events IS 'Context-only Instagram events received from Meta; never used for response delivery.';
COMMENT ON COLUMN kommo_message_jobs.context_status IS 'State of optional Meta context correlation for a Kommo job.';

INSERT INTO schema_migrations (version, name) VALUES
    (5, 'meta_instagram_context')
ON CONFLICT (version) DO NOTHING;

COMMIT;
