-- Upgrade any pre-consolidation store database to the current schema baseline.
BEGIN;

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS customer_channel_mappings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    provider VARCHAR NOT NULL,
    channel VARCHAR NOT NULL,
    external_contact_id VARCHAR,
    external_lead_id VARCHAR,
    external_chat_id VARCHAR,
    external_talk_id VARCHAR,
    external_author_id VARCHAR,
    external_origin VARCHAR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE customer_channel_mappings
    ADD COLUMN IF NOT EXISTS external_author_id VARCHAR,
    ADD COLUMN IF NOT EXISTS external_origin VARCHAR;
ALTER TABLE customer_channel_mappings
    DROP CONSTRAINT IF EXISTS customer_channel_mappings_any_external_id;
ALTER TABLE customer_channel_mappings
    ADD CONSTRAINT customer_channel_mappings_any_external_id CHECK (
        external_contact_id IS NOT NULL OR external_lead_id IS NOT NULL
        OR external_chat_id IS NOT NULL OR external_talk_id IS NOT NULL
        OR external_author_id IS NOT NULL
    );
DROP INDEX IF EXISTS uq_customer_channel_mappings_lead;
CREATE INDEX IF NOT EXISTS idx_customer_channel_mappings_lead
    ON customer_channel_mappings(provider, channel, external_lead_id, updated_at DESC)
    WHERE external_lead_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS broadcast_deliveries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    broadcast_id UUID NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
    customer_id UUID REFERENCES customers(id) ON DELETE SET NULL,
    platform_id TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'whatsapp',
    display_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    claimed_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (broadcast_id, channel, platform_id)
);
ALTER TABLE broadcast_deliveries
    ADD COLUMN IF NOT EXISTS outbound_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS meta_message_id TEXT;
ALTER TABLE broadcast_deliveries
    DROP CONSTRAINT IF EXISTS broadcast_deliveries_status_check;
ALTER TABLE broadcast_deliveries
    ADD CONSTRAINT broadcast_deliveries_status_check
    CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'delivery_unknown'));

CREATE TABLE IF NOT EXISTS meta_inbound_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    message_parts JSONB NOT NULL DEFAULT '[]'::jsonb,
    media_url TEXT,
    customer_profile JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending',
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    processing_started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE meta_inbound_jobs
    ADD COLUMN IF NOT EXISTS processing_heartbeat_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS processing_lease_token TEXT,
    ADD COLUMN IF NOT EXISTS outbound_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS outbound_message_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE TABLE IF NOT EXISTS payment_proof_replays (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proof_hash TEXT UNIQUE,
    reference_key TEXT UNIQUE,
    original_order_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (proof_hash IS NOT NULL OR reference_key IS NOT NULL)
);
ALTER TABLE payment_proof_replays ENABLE ROW LEVEL SECURITY;
REVOKE ALL PRIVILEGES ON payment_proof_replays FROM anon, authenticated;

INSERT INTO schema_migrations (version, name)
VALUES (1, 'legacy_pre_consolidation')
ON CONFLICT (version) DO NOTHING;
DELETE FROM schema_migrations WHERE version > 2;
INSERT INTO schema_migrations (version, name)
VALUES (2, 'consolidated_upgrade')
ON CONFLICT (version) DO NOTHING;

COMMIT;
