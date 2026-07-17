-- Kommo channel backend integration
-- Run after 001_schema.sql against each store database.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS customer_channel_mappings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    provider VARCHAR NOT NULL,
    channel VARCHAR NOT NULL,
    external_contact_id VARCHAR,
    external_lead_id VARCHAR,
    external_chat_id VARCHAR,
    external_talk_id VARCHAR,
    external_origin VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT customer_channel_mappings_any_external_id CHECK (
        external_contact_id IS NOT NULL
        OR external_lead_id IS NOT NULL
        OR external_chat_id IS NOT NULL
        OR external_talk_id IS NOT NULL
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_channel_mappings_contact
    ON customer_channel_mappings(provider, channel, external_contact_id)
    WHERE external_contact_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_channel_mappings_lead
    ON customer_channel_mappings(provider, channel, external_lead_id)
    WHERE external_lead_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_channel_mappings_chat
    ON customer_channel_mappings(provider, channel, external_chat_id)
    WHERE external_chat_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_channel_mappings_talk
    ON customer_channel_mappings(provider, channel, external_talk_id)
    WHERE external_talk_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_customer_channel_mappings_customer
    ON customer_channel_mappings(customer_id, provider, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_customer_channel_mappings_lookup
    ON customer_channel_mappings(provider, channel, updated_at DESC);

DROP TRIGGER IF EXISTS trg_customer_channel_mappings_updated_at ON customer_channel_mappings;
CREATE TRIGGER trg_customer_channel_mappings_updated_at
    BEFORE UPDATE ON customer_channel_mappings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS kommo_message_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id TEXT NOT NULL,
    external_message_id TEXT,
    lead_id TEXT,
    contact_id TEXT,
    chat_id TEXT,
    talk_id TEXT,
    origin TEXT,
    channel TEXT,
    combined_message TEXT NOT NULL,
    media_url TEXT,
    return_url TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    buffer_expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    salesbot_launched_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processing_started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CONSTRAINT kommo_message_jobs_status_check CHECK (
        status IN ('pending', 'waiting_for_salesbot', 'ready', 'processing', 'sent', 'discarded', 'failed')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_message_jobs_external_message
    ON kommo_message_jobs(external_message_id)
    WHERE external_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kommo_message_jobs_status_due
    ON kommo_message_jobs(status, buffer_expires_at, created_at);
CREATE INDEX IF NOT EXISTS idx_kommo_message_jobs_correlation
    ON kommo_message_jobs(correlation_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_kommo_message_jobs_lead_status
    ON kommo_message_jobs(lead_id, status, updated_at DESC)
    WHERE lead_id IS NOT NULL;
DROP INDEX IF EXISTS uq_kommo_message_jobs_active_salesbot;
CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_message_jobs_active_salesbot
    ON kommo_message_jobs(correlation_id)
    WHERE status IN ('waiting_for_salesbot', 'ready', 'processing');
CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_message_jobs_pending_correlation
    ON kommo_message_jobs(correlation_id)
    WHERE status = 'pending';

DROP TRIGGER IF EXISTS trg_kommo_message_jobs_updated_at ON kommo_message_jobs;
CREATE TRIGGER trg_kommo_message_jobs_updated_at
    BEFORE UPDATE ON kommo_message_jobs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE kommo_message_jobs IS 'Durable Kommo inbound jobs. Failed jobs are safe to retry only after confirming no sent job exists for the same external_message_id/correlation_id.';
