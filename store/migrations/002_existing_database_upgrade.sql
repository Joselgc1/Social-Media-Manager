-- Upgrade store databases created from an older 001_schema.sql.
-- Do not run 001_schema.sql again on an existing database.
BEGIN;

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Columns added after the original store schema shipped. Add these before any
-- constraints or indexes that reference them.
ALTER TABLE customers
    ADD COLUMN IF NOT EXISTS escalation_source TEXT,
    ADD COLUMN IF NOT EXISTS escalated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS escalation_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS is_blocked BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS last_shipping_address TEXT,
    ADD COLUMN IF NOT EXISTS last_shipping_city TEXT,
    ADD COLUMN IF NOT EXISTS last_shipping_method TEXT;

ALTER TABLE customers
    DROP CONSTRAINT IF EXISTS customers_escalation_source_check;
ALTER TABLE customers
    ADD CONSTRAINT customers_escalation_source_check CHECK (
        escalation_source IS NULL
        OR escalation_source IN ('automatic', 'manual', 'external')
    );

ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS source_id TEXT;

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'USD',
    ADD COLUMN IF NOT EXISTS payment_proof_hash TEXT,
    ADD COLUMN IF NOT EXISTS payment_reference TEXT,
    ADD COLUMN IF NOT EXISTS payment_reference_key TEXT,
    ADD COLUMN IF NOT EXISTS payment_currency TEXT,
    ADD COLUMN IF NOT EXISTS payment_amount NUMERIC(14,2),
    ADD COLUMN IF NOT EXISTS payment_transaction_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS payment_verified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS inventory_status TEXT NOT NULL DEFAULT 'legacy_unknown',
    ADD COLUMN IF NOT EXISTS inventory_reserved_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS inventory_released_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS customer_totals_applied BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS conversation_sessions (
    customer_id UUID PRIMARY KEY REFERENCES customers(id) ON DELETE CASCADE,
    active_agent TEXT NOT NULL DEFAULT 'legacy',
    active_intent TEXT,
    workflow_stage TEXT NOT NULL DEFAULT 'idle',
    checkout_draft JSONB NOT NULL DEFAULT '{}'::jsonb,
    current_order_id UUID REFERENCES orders(id) ON DELETE SET NULL,
    last_route_confidence NUMERIC(4,3),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ai_run_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID REFERENCES customers(id) ON DELETE SET NULL,
    channel TEXT,
    orchestration_mode TEXT NOT NULL DEFAULT 'legacy',
    selected_agent TEXT NOT NULL DEFAULT 'legacy',
    route_intent TEXT,
    route_source TEXT,
    route_confidence NUMERIC(4,3),
    provider TEXT,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    response_time_ms INTEGER,
    tool_names JSONB NOT NULL DEFAULT '[]'::jsonb,
    tool_rounds INTEGER NOT NULL DEFAULT 0,
    handoff_occurred BOOLEAN NOT NULL DEFAULT FALSE,
    fallback_occurred BOOLEAN NOT NULL DEFAULT FALSE,
    escalation_occurred BOOLEAN NOT NULL DEFAULT FALSE,
    shadow_evaluation BOOLEAN NOT NULL DEFAULT FALSE,
    legacy_fallback BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
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
    ADD COLUMN IF NOT EXISTS external_contact_id VARCHAR,
    ADD COLUMN IF NOT EXISTS external_lead_id VARCHAR,
    ADD COLUMN IF NOT EXISTS external_chat_id VARCHAR,
    ADD COLUMN IF NOT EXISTS external_talk_id VARCHAR,
    ADD COLUMN IF NOT EXISTS external_author_id VARCHAR,
    ADD COLUMN IF NOT EXISTS external_origin VARCHAR,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

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

CREATE TABLE IF NOT EXISTS meta_inbound_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel TEXT NOT NULL CHECK (channel IN ('whatsapp', 'instagram')),
    sender_id TEXT NOT NULL,
    message_parts JSONB NOT NULL DEFAULT '[]'::jsonb,
    media_url TEXT,
    customer_profile JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    processing_started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS meta_inbound_receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel TEXT NOT NULL CHECK (channel IN ('whatsapp', 'instagram')),
    external_message_id TEXT NOT NULL,
    job_id UUID NOT NULL REFERENCES meta_inbound_jobs(id) ON DELETE CASCADE,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(channel, external_message_id)
);

CREATE TABLE IF NOT EXISTS kommo_message_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id TEXT NOT NULL,
    external_message_id TEXT,
    lead_id TEXT,
    contact_id TEXT,
    chat_id TEXT,
    talk_id TEXT,
    author_id TEXT,
    author_name TEXT,
    author_username TEXT,
    author_profile_url TEXT,
    sender_username TEXT,
    sender_profile_url TEXT,
    origin TEXT,
    channel TEXT,
    interaction_type TEXT NOT NULL DEFAULT 'private_message',
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
    processing_lease_id UUID,
    ai_started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    callback_claims JSONB,
    public_comment_context JSONB,
    salesbot_token_jti TEXT,
    salesbot_account_id TEXT,
    salesbot_user_id TEXT,
    salesbot_client_uuid TEXT,
    continuation_payload JSONB,
    continuation_response JSONB,
    assistant_message_persisted_at TIMESTAMPTZ
);

ALTER TABLE kommo_message_jobs
    ADD COLUMN IF NOT EXISTS external_message_id TEXT,
    ADD COLUMN IF NOT EXISTS lead_id TEXT,
    ADD COLUMN IF NOT EXISTS contact_id TEXT,
    ADD COLUMN IF NOT EXISTS chat_id TEXT,
    ADD COLUMN IF NOT EXISTS talk_id TEXT,
    ADD COLUMN IF NOT EXISTS author_id TEXT,
    ADD COLUMN IF NOT EXISTS author_name TEXT,
    ADD COLUMN IF NOT EXISTS author_username TEXT,
    ADD COLUMN IF NOT EXISTS author_profile_url TEXT,
    ADD COLUMN IF NOT EXISTS sender_username TEXT,
    ADD COLUMN IF NOT EXISTS sender_profile_url TEXT,
    ADD COLUMN IF NOT EXISTS origin TEXT,
    ADD COLUMN IF NOT EXISTS channel TEXT,
    ADD COLUMN IF NOT EXISTS interaction_type TEXT NOT NULL DEFAULT 'private_message',
    ADD COLUMN IF NOT EXISTS media_url TEXT,
    ADD COLUMN IF NOT EXISTS return_url TEXT,
    ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_error TEXT,
    ADD COLUMN IF NOT EXISTS buffer_expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS salesbot_launched_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS processing_lease_id UUID,
    ADD COLUMN IF NOT EXISTS ai_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS callback_claims JSONB,
    ADD COLUMN IF NOT EXISTS public_comment_context JSONB,
    ADD COLUMN IF NOT EXISTS salesbot_token_jti TEXT,
    ADD COLUMN IF NOT EXISTS salesbot_account_id TEXT,
    ADD COLUMN IF NOT EXISTS salesbot_user_id TEXT,
    ADD COLUMN IF NOT EXISTS salesbot_client_uuid TEXT,
    ADD COLUMN IF NOT EXISTS continuation_payload JSONB,
    ADD COLUMN IF NOT EXISTS continuation_response JSONB,
    ADD COLUMN IF NOT EXISTS assistant_message_persisted_at TIMESTAMPTZ;

ALTER TABLE kommo_message_jobs
    DROP CONSTRAINT IF EXISTS kommo_message_jobs_status_check;
ALTER TABLE kommo_message_jobs
    ADD CONSTRAINT kommo_message_jobs_status_check CHECK (
        status IN (
            'pending', 'prepared', 'waiting_for_salesbot', 'ready', 'processing',
            'continuing', 'sent', 'discarded', 'delivery_unknown', 'failed'
        )
    );
ALTER TABLE kommo_message_jobs
    DROP CONSTRAINT IF EXISTS kommo_message_jobs_interaction_type_check;
ALTER TABLE kommo_message_jobs
    ADD CONSTRAINT kommo_message_jobs_interaction_type_check CHECK (
        interaction_type IN ('private_message', 'instagram_comment')
    );

CREATE TABLE IF NOT EXISTS kommo_message_receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_message_id TEXT NOT NULL,
    job_id UUID REFERENCES kommo_message_jobs(id) ON DELETE SET NULL,
    correlation_id TEXT NOT NULL,
    lead_id TEXT,
    contact_id TEXT,
    chat_id TEXT,
    talk_id TEXT,
    author_id TEXT,
    origin TEXT,
    channel TEXT,
    interaction_type TEXT NOT NULL DEFAULT 'private_message',
    receipt_status TEXT NOT NULL DEFAULT 'created',
    received_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE kommo_message_receipts
    ADD COLUMN IF NOT EXISTS author_id TEXT,
    ADD COLUMN IF NOT EXISTS interaction_type TEXT NOT NULL DEFAULT 'private_message';
ALTER TABLE kommo_message_receipts
    DROP CONSTRAINT IF EXISTS kommo_message_receipts_status_check;
ALTER TABLE kommo_message_receipts
    ADD CONSTRAINT kommo_message_receipts_status_check CHECK (receipt_status IN ('created', 'merged'));
ALTER TABLE kommo_message_receipts
    DROP CONSTRAINT IF EXISTS kommo_message_receipts_interaction_type_check;
ALTER TABLE kommo_message_receipts
    ADD CONSTRAINT kommo_message_receipts_interaction_type_check CHECK (
        interaction_type IN ('private_message', 'instagram_comment')
    );

-- Current indexes and triggers. All are safe to create after the columns above.
CREATE INDEX IF NOT EXISTS idx_customers_expired_automatic_escalations
    ON customers(escalation_expires_at ASC, id)
    WHERE conversation_state = 'escalated'
      AND escalation_source = 'automatic'
      AND escalation_expires_at IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_conversations_delivery_source
    ON conversations(channel, role, source_id) WHERE source_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_payment_proof_hash_unique
    ON orders(payment_proof_hash) WHERE payment_proof_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_payment_reference_key_unique
    ON orders(payment_reference_key) WHERE payment_reference_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_orders_inventory_reservations
    ON orders(inventory_status, created_at) WHERE inventory_status = 'reserved';
CREATE INDEX IF NOT EXISTS idx_conversation_sessions_updated
    ON conversation_sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_run_logs_created ON ai_run_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_run_logs_customer ON ai_run_logs(customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_customer_channel_mappings_contact
    ON customer_channel_mappings(provider, channel, external_contact_id, updated_at DESC)
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
CREATE INDEX IF NOT EXISTS idx_customer_channel_mappings_author
    ON customer_channel_mappings(provider, channel, external_author_id, updated_at DESC)
    WHERE external_author_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_customer_channel_mappings_customer
    ON customer_channel_mappings(customer_id, provider, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_meta_inbound_jobs_pending_sender
    ON meta_inbound_jobs(channel, sender_id) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS uq_meta_inbound_jobs_processing_sender
    ON meta_inbound_jobs(channel, sender_id) WHERE status = 'processing';
CREATE INDEX IF NOT EXISTS idx_meta_inbound_jobs_due
    ON meta_inbound_jobs(status, available_at, created_at);
CREATE INDEX IF NOT EXISTS idx_meta_inbound_receipts_job ON meta_inbound_receipts(job_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_message_jobs_external_message
    ON kommo_message_jobs(external_message_id) WHERE external_message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kommo_message_jobs_status_due
    ON kommo_message_jobs(status, buffer_expires_at, created_at);
CREATE INDEX IF NOT EXISTS idx_kommo_message_jobs_correlation
    ON kommo_message_jobs(correlation_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_kommo_message_jobs_lead_status
    ON kommo_message_jobs(lead_id, status, updated_at DESC) WHERE lead_id IS NOT NULL;
DROP INDEX IF EXISTS uq_kommo_message_jobs_active_salesbot;
CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_message_jobs_active_salesbot
    ON kommo_message_jobs(correlation_id)
    WHERE status IN ('prepared', 'waiting_for_salesbot', 'ready', 'processing', 'continuing');
CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_message_jobs_pending_correlation
    ON kommo_message_jobs(correlation_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_kommo_message_jobs_delivery_unknown
    ON kommo_message_jobs(status, updated_at DESC) WHERE status = 'delivery_unknown';
CREATE INDEX IF NOT EXISTS idx_kommo_message_jobs_salesbot_token_jti
    ON kommo_message_jobs(salesbot_token_jti) WHERE salesbot_token_jti IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_message_receipts_external_message
    ON kommo_message_receipts(external_message_id);
CREATE INDEX IF NOT EXISTS idx_kommo_message_receipts_job
    ON kommo_message_receipts(job_id, created_at DESC);

DROP TRIGGER IF EXISTS trg_customer_channel_mappings_updated_at ON customer_channel_mappings;
CREATE TRIGGER trg_customer_channel_mappings_updated_at
    BEFORE UPDATE ON customer_channel_mappings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
DROP TRIGGER IF EXISTS trg_kommo_message_jobs_updated_at ON kommo_message_jobs;
CREATE TRIGGER trg_kommo_message_jobs_updated_at
    BEFORE UPDATE ON kommo_message_jobs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

INSERT INTO kommo_message_receipts (
    external_message_id, job_id, correlation_id, lead_id, contact_id, chat_id,
    talk_id, author_id, origin, channel, interaction_type, receipt_status, received_at, created_at
)
SELECT
    external_message_id, id, correlation_id, lead_id, contact_id, chat_id,
    talk_id, author_id, origin, channel, COALESCE(interaction_type, 'private_message'),
    CASE
        WHEN status = 'discarded' AND COALESCE(last_error, '') LIKE 'Merged into %' THEN 'merged'
        ELSE 'created'
    END,
    created_at, created_at
FROM kommo_message_jobs
WHERE external_message_id IS NOT NULL
ON CONFLICT (external_message_id) DO NOTHING;

-- The backend uses the database owner connection. Data API roles receive no
-- policies and no object privileges.
ALTER TABLE customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversation_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE broadcasts ENABLE ROW LEVEL SECURITY;
ALTER TABLE settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_run_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE daily_analytics ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_analytics ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_channel_mappings ENABLE ROW LEVEL SECURITY;
ALTER TABLE meta_inbound_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE meta_inbound_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE kommo_message_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE kommo_message_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM anon, authenticated;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL PRIVILEGES ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL PRIVILEGES ON SEQUENCES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.set_updated_at() FROM PUBLIC, anon, authenticated;

INSERT INTO schema_migrations (version, name) VALUES
    (1, 'legacy_unversioned_baseline'),
    (2, 'versioned_schema_and_security_hardening')
ON CONFLICT (version) DO NOTHING;

COMMIT;
