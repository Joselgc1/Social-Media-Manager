-- Kommo customer profile enrichment metadata
-- Run manually against existing store databases after 001_schema.sql.

ALTER TABLE customer_channel_mappings
    ADD COLUMN IF NOT EXISTS external_author_id VARCHAR;

ALTER TABLE customer_channel_mappings
    DROP CONSTRAINT IF EXISTS customer_channel_mappings_any_external_id;

ALTER TABLE customer_channel_mappings
    ADD CONSTRAINT customer_channel_mappings_any_external_id CHECK (
        external_contact_id IS NOT NULL
        OR external_lead_id IS NOT NULL
        OR external_chat_id IS NOT NULL
        OR external_talk_id IS NOT NULL
        OR external_author_id IS NOT NULL
    );

CREATE INDEX IF NOT EXISTS idx_customer_channel_mappings_author
    ON customer_channel_mappings(provider, channel, external_author_id, updated_at DESC)
    WHERE external_author_id IS NOT NULL;

ALTER TABLE kommo_message_jobs
    ADD COLUMN IF NOT EXISTS author_id TEXT,
    ADD COLUMN IF NOT EXISTS author_name TEXT;

CREATE INDEX IF NOT EXISTS idx_kommo_message_jobs_author
    ON kommo_message_jobs(author_id, updated_at DESC)
    WHERE author_id IS NOT NULL;

ALTER TABLE kommo_message_receipts
    ADD COLUMN IF NOT EXISTS author_id TEXT;

CREATE INDEX IF NOT EXISTS idx_kommo_message_receipts_author
    ON kommo_message_receipts(author_id, created_at DESC)
    WHERE author_id IS NOT NULL;

UPDATE kommo_message_receipts receipt
SET author_id = job.author_id
FROM kommo_message_jobs job
WHERE receipt.job_id = job.id
  AND receipt.author_id IS NULL
  AND job.author_id IS NOT NULL;
