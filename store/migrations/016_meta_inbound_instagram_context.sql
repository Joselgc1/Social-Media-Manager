-- Carry native Instagram interaction scope and durable event metadata in Meta jobs.
BEGIN;

ALTER TABLE meta_inbound_jobs
    ADD COLUMN IF NOT EXISTS interaction_type TEXT NOT NULL DEFAULT 'private_message',
    ADD COLUMN IF NOT EXISTS integration_context JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE meta_inbound_jobs
    DROP CONSTRAINT IF EXISTS meta_inbound_jobs_interaction_type_check;
ALTER TABLE meta_inbound_jobs
    ADD CONSTRAINT meta_inbound_jobs_interaction_type_check CHECK (
        interaction_type IN ('private_message', 'instagram_comment')
    );

DROP INDEX IF EXISTS uq_meta_inbound_jobs_pending_sender;
CREATE INDEX IF NOT EXISTS idx_meta_inbound_jobs_pending_sender_scope
    ON meta_inbound_jobs(channel, sender_id, interaction_type, created_at)
    WHERE status = 'pending';

INSERT INTO schema_migrations (version, name) VALUES
    (16, 'meta_inbound_instagram_context')
ON CONFLICT (version) DO NOTHING;

COMMIT;
