-- Isolate private-message and Instagram-comment model history while preserving customer identity.
BEGIN;

ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS interaction_type TEXT NOT NULL DEFAULT 'private_message';

ALTER TABLE conversations
    DROP CONSTRAINT IF EXISTS conversations_interaction_type_check;
ALTER TABLE conversations
    ADD CONSTRAINT conversations_interaction_type_check CHECK (
        interaction_type IN ('private_message', 'instagram_comment')
    );

UPDATE conversations conversation
SET interaction_type = job.interaction_type
FROM kommo_message_jobs job
WHERE conversation.source_id = 'kommo-job:' || job.id::text
  AND job.interaction_type IN ('private_message', 'instagram_comment')
  AND conversation.interaction_type IS DISTINCT FROM job.interaction_type;

CREATE INDEX IF NOT EXISTS idx_conv_customer_interaction
    ON conversations(customer_id, interaction_type, created_at DESC);

INSERT INTO schema_migrations (version, name) VALUES
    (15, 'conversation_interaction_scope')
ON CONFLICT (version) DO NOTHING;

COMMIT;
