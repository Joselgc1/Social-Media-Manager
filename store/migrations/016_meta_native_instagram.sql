-- Complete the transition to Meta-native Instagram messaging and comments.
BEGIN;

-- Carry native Instagram interaction scope and durable event metadata in Meta jobs.
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

-- Preserve ordered native Instagram attachments and human/AI message provenance.
ALTER TABLE meta_inbound_jobs
    ADD COLUMN IF NOT EXISTS inbound_attachments JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS author_type TEXT,
    ADD COLUMN IF NOT EXISTS provider_message_id TEXT;

UPDATE conversations
SET author_type = CASE WHEN role = 'user' THEN 'customer' ELSE 'ai' END
WHERE author_type IS NULL;

ALTER TABLE conversations
    ALTER COLUMN author_type SET DEFAULT 'ai',
    ALTER COLUMN author_type SET NOT NULL;

ALTER TABLE conversations
    DROP CONSTRAINT IF EXISTS conversations_author_type_check;
ALTER TABLE conversations
    ADD CONSTRAINT conversations_author_type_check CHECK (
        author_type IN ('customer', 'ai', 'human')
    );

CREATE UNIQUE INDEX IF NOT EXISTS uq_conversations_provider_message
    ON conversations(channel, provider_message_id)
    WHERE provider_message_id IS NOT NULL;

-- Retire live Instagram/Kommo processing without dropping historical correlation data.
UPDATE kommo_message_jobs
SET status = 'discarded',
    last_error = 'instagram_transport_retired',
    processing_lease_id = NULL,
    completed_at = COALESCE(completed_at, NOW()),
    updated_at = NOW()
WHERE channel = 'instagram'
  AND status IN ('pending', 'prepared')
  AND salesbot_launched_at IS NULL
  AND return_url IS NULL
  AND ai_started_at IS NULL;

-- A SQL migration cannot safely complete an external Salesbot continuation.
-- Keep the old deployment serving until launched or uncertain work drains.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM kommo_message_jobs
        WHERE channel = 'instagram'
          AND status IN (
              'pending', 'prepared', 'waiting_for_salesbot', 'waiting_for_context',
              'ready', 'processing', 'continuing'
          )
    ) THEN
        RAISE EXCEPTION
            'Instagram Kommo jobs are still active; disable Instagram Kommo ingress and retry after they drain';
    END IF;
END $$;

UPDATE meta_instagram_context_events
SET correlation_status = 'expired',
    correlation_details = correlation_details || jsonb_build_object(
        'deprecated_reason', 'instagram_transport_is_meta_native'
    ),
    updated_at = NOW()
WHERE correlation_status IN ('pending', 'ambiguous');

DELETE FROM settings WHERE key = 'kommo_emoji_mode_instagram';

-- Add outbound-echo reconciliation and public-comment thread isolation.
ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS instagram_media_id TEXT,
    ADD COLUMN IF NOT EXISTS instagram_comment_id TEXT,
    ADD COLUMN IF NOT EXISTS instagram_parent_comment_id TEXT,
    ADD COLUMN IF NOT EXISTS instagram_thread_id TEXT;

CREATE INDEX IF NOT EXISTS idx_conv_instagram_comment_thread
    ON conversations(
        customer_id,
        instagram_media_id,
        instagram_thread_id,
        created_at DESC
    )
    WHERE interaction_type = 'instagram_comment';

CREATE INDEX IF NOT EXISTS idx_conv_instagram_comment_id
    ON conversations(instagram_media_id, instagram_comment_id)
    WHERE interaction_type = 'instagram_comment'
      AND instagram_comment_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS meta_instagram_outbound_echoes (
    provider_message_id TEXT PRIMARY KEY,
    recipient_id TEXT NOT NULL,
    message_text TEXT NOT NULL DEFAULT '',
    classification TEXT NOT NULL DEFAULT 'pending'
        CHECK (classification IN ('pending', 'backend', 'manual', 'unknown')),
    eligible_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '20 seconds'),
    echo_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reconciled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_meta_instagram_echoes_pending
    ON meta_instagram_outbound_echoes(eligible_at, echo_seen_at)
    WHERE classification = 'pending';

ALTER TABLE meta_instagram_outbound_echoes ENABLE ROW LEVEL SECURITY;
REVOKE ALL PRIVILEGES ON TABLE meta_instagram_outbound_echoes FROM PUBLIC;

INSERT INTO schema_migrations (version, name) VALUES
    (16, 'meta_native_instagram')
ON CONFLICT (version) DO NOTHING;

COMMIT;
