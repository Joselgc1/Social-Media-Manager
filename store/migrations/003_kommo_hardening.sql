-- Kommo hardening: durable callback claims, continuation tracking, and lead-safe mappings.
-- Run after 002_kommo_integration.sql against each store database.

ALTER TABLE kommo_message_jobs
    ADD COLUMN IF NOT EXISTS callback_claims JSONB,
    ADD COLUMN IF NOT EXISTS salesbot_token_jti TEXT,
    ADD COLUMN IF NOT EXISTS salesbot_account_id TEXT,
    ADD COLUMN IF NOT EXISTS salesbot_user_id TEXT,
    ADD COLUMN IF NOT EXISTS salesbot_client_uuid TEXT,
    ADD COLUMN IF NOT EXISTS continuation_payload JSONB,
    ADD COLUMN IF NOT EXISTS continuation_response JSONB;

ALTER TABLE kommo_message_jobs
    DROP CONSTRAINT IF EXISTS kommo_message_jobs_status_check;

ALTER TABLE kommo_message_jobs
    ADD CONSTRAINT kommo_message_jobs_status_check CHECK (
        status IN (
            'pending',
            'prepared',
            'waiting_for_salesbot',
            'ready',
            'processing',
            'continuing',
            'sent',
            'discarded',
            'delivery_unknown',
            'failed'
        )
    );

DROP INDEX IF EXISTS uq_customer_channel_mappings_contact;
CREATE INDEX IF NOT EXISTS idx_customer_channel_mappings_contact
    ON customer_channel_mappings(provider, channel, external_contact_id, updated_at DESC)
    WHERE external_contact_id IS NOT NULL;

DROP INDEX IF EXISTS uq_kommo_message_jobs_active_salesbot;
CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_message_jobs_active_salesbot
    ON kommo_message_jobs(correlation_id)
    WHERE status IN ('prepared', 'waiting_for_salesbot', 'ready', 'processing', 'continuing');

CREATE INDEX IF NOT EXISTS idx_kommo_message_jobs_delivery_unknown
    ON kommo_message_jobs(status, updated_at DESC)
    WHERE status = 'delivery_unknown';

CREATE INDEX IF NOT EXISTS idx_kommo_message_jobs_salesbot_token_jti
    ON kommo_message_jobs(salesbot_token_jti)
    WHERE salesbot_token_jti IS NOT NULL;

COMMENT ON COLUMN kommo_message_jobs.continuation_payload IS 'Last Salesbot continuation payload attempted for this job.';
COMMENT ON COLUMN kommo_message_jobs.continuation_response IS 'Sanitized Salesbot continuation response when Kommo accepted the request.';
