-- Add per-message Kommo receipt text for safe Story correlation.
BEGIN;

ALTER TABLE kommo_message_receipts
    ADD COLUMN IF NOT EXISTS message_text TEXT,
    ADD COLUMN IF NOT EXISTS normalized_text_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_kommo_receipts_story_text
    ON kommo_message_receipts(job_id, normalized_text_hash, received_at, created_at)
    WHERE channel = 'instagram'
      AND interaction_type = 'private_message'
      AND normalized_text_hash IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_meta_instagram_sender
    ON customer_channel_mappings(provider, channel, external_author_id)
    WHERE provider = 'meta'
      AND channel = 'instagram'
      AND external_author_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_meta_instagram_identity
    ON customer_channel_mappings(customer_id, provider, channel)
    WHERE provider = 'meta'
      AND channel = 'instagram'
      AND external_author_id IS NOT NULL;

INSERT INTO schema_migrations (version, name) VALUES
    (7, 'instagram_story_production_fixes')
ON CONFLICT (version) DO NOTHING;

COMMIT;
