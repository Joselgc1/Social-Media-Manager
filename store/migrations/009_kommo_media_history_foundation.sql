-- Add transport-independent media history and outbound delivery state.
BEGIN;

ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS attachments JSONB;

COMMENT ON COLUMN conversations.attachments IS
    'Transport-independent semantic attachments used to reconstruct LLM context. Never store Kommo Drive UUIDs or delivery metadata here.';

CREATE TABLE IF NOT EXISTS kommo_outbound_deliveries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID NOT NULL REFERENCES kommo_message_jobs(id) ON DELETE CASCADE,
    transport TEXT NOT NULL CHECK (transport IN ('salesbot', 'chats_api')),
    media_type TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'prepared', 'sending', 'accepted', 'confirmed', 'failed', 'delivery_unknown')
    ),
    provider_message_id TEXT,
    request_fingerprint TEXT,
    attachment_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    accepted_at TIMESTAMPTZ,
    confirmed_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kommo_outbound_deliveries_job
    ON kommo_outbound_deliveries(job_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kommo_outbound_deliveries_status
    ON kommo_outbound_deliveries(status, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_outbound_deliveries_salesbot_job
    ON kommo_outbound_deliveries(job_id)
    WHERE transport = 'salesbot';
CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_outbound_deliveries_provider_message
    ON kommo_outbound_deliveries(transport, provider_message_id)
    WHERE provider_message_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_outbound_deliveries_request
    ON kommo_outbound_deliveries(job_id, transport, request_fingerprint)
    WHERE request_fingerprint IS NOT NULL;

DROP TRIGGER IF EXISTS trg_kommo_outbound_deliveries_updated_at ON kommo_outbound_deliveries;
CREATE TRIGGER trg_kommo_outbound_deliveries_updated_at
    BEFORE UPDATE ON kommo_outbound_deliveries
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE kommo_outbound_deliveries IS
    'Transport-specific outbound delivery state. Semantic media history belongs in conversations.attachments.';
COMMENT ON COLUMN kommo_outbound_deliveries.attachment_metadata IS
    'Transport/provider attachment metadata; never include this JSON in LLM conversation history.';

ALTER TABLE kommo_outbound_deliveries ENABLE ROW LEVEL SECURITY;
REVOKE ALL PRIVILEGES ON kommo_outbound_deliveries FROM PUBLIC;

INSERT INTO schema_migrations (version, name) VALUES
    (9, 'kommo_media_history_foundation')
ON CONFLICT (version) DO NOTHING;

COMMIT;
