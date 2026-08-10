-- Native Instagram audio metadata and human/AI message provenance.
BEGIN;

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

INSERT INTO schema_migrations (version, name) VALUES
    (17, 'meta_instagram_operations')
ON CONFLICT (version) DO NOTHING;

COMMIT;
