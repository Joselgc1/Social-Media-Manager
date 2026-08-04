-- Full store schema
-- Single consolidated schema for fresh installs, including multi-agent workflow state,
-- AI run observability, Kommo integration tables, and hardening.
-- Run this against your PostgreSQL database.

BEGIN;

-- Enable UUID generation
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

-- ============================================================
-- Customers
-- ============================================================
CREATE TABLE IF NOT EXISTS customers (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel               TEXT NOT NULL,               -- "instagram" or "whatsapp"
    platform_id           TEXT NOT NULL,               -- IG-scoped ID or WhatsApp phone number
    display_name          TEXT,
    phone                 TEXT,                        -- For WhatsApp this IS the platform_id
    instagram_handle      TEXT,
    tags                  JSONB DEFAULT '[]'::jsonb,   -- ["vip", "interested:pajamas", "size:M"]
    preferred_sizes       JSONB DEFAULT '[]'::jsonb,
    total_orders          INTEGER DEFAULT 0,
    total_spent           NUMERIC(10,2) DEFAULT 0,
    first_contact         TIMESTAMPTZ DEFAULT NOW(),
    last_active           TIMESTAMPTZ DEFAULT NOW(),
    notes                 TEXT,
    conversation_state    TEXT DEFAULT 'active',       -- "active", "escalated", "blocked"
    escalation_source     TEXT,                         -- "automatic", "manual", "external"
    escalated_at          TIMESTAMPTZ,
    escalation_expires_at TIMESTAMPTZ,
    is_blocked            BOOLEAN DEFAULT FALSE,
    last_shipping_address TEXT,
    last_shipping_city    TEXT,
    last_shipping_method  TEXT,
    marketing_opt_in      BOOLEAN NOT NULL DEFAULT FALSE,
    marketing_opt_in_at   TIMESTAMPTZ,
    marketing_opt_out_at  TIMESTAMPTZ,
    UNIQUE(channel, platform_id)
);

ALTER TABLE customers
    DROP CONSTRAINT IF EXISTS customers_escalation_source_check;

ALTER TABLE customers
    ADD CONSTRAINT customers_escalation_source_check CHECK (
        escalation_source IS NULL
        OR escalation_source IN ('automatic', 'manual', 'external')
    );

CREATE INDEX IF NOT EXISTS idx_customers_tags ON customers USING gin(tags);
CREATE INDEX IF NOT EXISTS idx_customers_last_active ON customers(last_active DESC);
CREATE INDEX IF NOT EXISTS idx_customers_marketing_whatsapp
    ON customers(last_active DESC)
    WHERE channel = 'whatsapp' AND marketing_opt_in = TRUE;
CREATE INDEX IF NOT EXISTS idx_customers_expired_automatic_escalations
    ON customers(escalation_expires_at ASC, id)
    WHERE conversation_state = 'escalated'
      AND escalation_source = 'automatic'
      AND escalation_expires_at IS NOT NULL;

-- ============================================================
-- Conversations (message history)
-- ============================================================
CREATE TABLE IF NOT EXISTS conversations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id     UUID REFERENCES customers(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,               -- "user" or "assistant"
    content         TEXT NOT NULL,
    channel         TEXT NOT NULL,
    media_url       TEXT,
    attachments     JSONB,                       -- Transport-independent semantic attachments
    function_calls  JSONB,                       -- Log of any tools the AI invoked
    source_id       TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS source_id TEXT,
    ADD COLUMN IF NOT EXISTS attachments JSONB;

CREATE INDEX IF NOT EXISTS idx_conv_customer ON conversations(customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conv_created ON conversations(created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_conversations_delivery_source
    ON conversations(channel, role, source_id)
    WHERE source_id IS NOT NULL;

-- ============================================================
-- Orders
-- ============================================================
CREATE TABLE IF NOT EXISTS orders (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id      UUID REFERENCES customers(id) ON DELETE SET NULL,
    items            JSONB NOT NULL,              -- [{"name": "...", "sku": "...", "size": "M", "qty": 1, "price": 28}]
    total            NUMERIC(10,2) NOT NULL,
    currency         TEXT NOT NULL DEFAULT 'USD',
    payment_method   TEXT,                        -- Store-defined payment method name
    payment_status   TEXT DEFAULT 'pending',      -- "pending", "proof_received", "confirmed", "failed"
    payment_proof    TEXT,                        -- URL to payment screenshot
    payment_proof_hash TEXT,
    payment_reference TEXT,
    payment_reference_key TEXT,
    payment_currency TEXT,
    payment_amount NUMERIC(14,2),
    payment_transaction_at TIMESTAMPTZ,
    payment_verified_at TIMESTAMPTZ,
    inventory_status TEXT NOT NULL DEFAULT 'legacy_unknown',
    inventory_reserved_at TIMESTAMPTZ,
    inventory_released_at TIMESTAMPTZ,
    customer_totals_applied BOOLEAN NOT NULL DEFAULT FALSE,
    shipping_method  TEXT,                        -- "mrw" or "zoom"
    shipping_city    TEXT,
    shipping_address TEXT,
    shipping_status  TEXT DEFAULT 'pending',      -- "pending", "shipped", "delivered"
    tracking_number  TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(payment_status, shipping_status);

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
    ADD COLUMN IF NOT EXISTS inventory_released_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_payment_proof_hash_unique
    ON orders(payment_proof_hash) WHERE payment_proof_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_payment_reference_key_unique
    ON orders(payment_reference_key) WHERE payment_reference_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_orders_inventory_reservations
    ON orders(inventory_status, created_at)
    WHERE inventory_status = 'reserved';

-- ============================================================
-- Conversation workflow state
-- ============================================================
CREATE TABLE IF NOT EXISTS conversation_sessions (
    customer_id           UUID PRIMARY KEY REFERENCES customers(id) ON DELETE CASCADE,
    active_agent          TEXT NOT NULL DEFAULT 'legacy',
    active_intent         TEXT,
    workflow_stage        TEXT NOT NULL DEFAULT 'idle',
    checkout_draft        JSONB NOT NULL DEFAULT '{}'::jsonb,
    current_order_id      UUID REFERENCES orders(id) ON DELETE SET NULL,
    last_route_confidence NUMERIC(4,3),
    instagram_content_context JSONB NOT NULL DEFAULT '{}'::jsonb,
    instagram_context_expires_at TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversation_sessions_active_agent
    ON conversation_sessions(active_agent);

CREATE INDEX IF NOT EXISTS idx_conversation_sessions_updated
    ON conversation_sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_sessions_instagram_context_expiry
    ON conversation_sessions(instagram_context_expires_at)
    WHERE instagram_context_expires_at IS NOT NULL;

-- ============================================================
-- Broadcasts
-- ============================================================
CREATE TABLE IF NOT EXISTS broadcasts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    template_name   TEXT NOT NULL,
    template_params JSONB,
    target_tags     JSONB NOT NULL,              -- ["vip", "interested:pajamas"]
    target_channel  TEXT DEFAULT 'whatsapp',
    scheduled_at    TIMESTAMPTZ,
    sent_at         TIMESTAMPTZ,
    audience_seeded_at TIMESTAMPTZ,
    recipients      INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'draft'         -- "draft", "scheduled", "sending", "sent", "partial", "failed"
);

CREATE TABLE IF NOT EXISTS broadcast_deliveries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    broadcast_id    UUID NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
    customer_id     UUID REFERENCES customers(id) ON DELETE SET NULL,
    platform_id     TEXT NOT NULL,
    channel         TEXT NOT NULL DEFAULT 'whatsapp' CHECK (channel = 'whatsapp'),
    display_name    TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'delivery_unknown')),
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    claimed_at      TIMESTAMPTZ,
    outbound_started_at TIMESTAMPTZ,
    meta_message_id TEXT,
    sent_at         TIMESTAMPTZ,
    failed_at       TIMESTAMPTZ,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (broadcast_id, channel, platform_id)
);

CREATE INDEX IF NOT EXISTS idx_broadcast_deliveries_claim
    ON broadcast_deliveries(broadcast_id, status, created_at);

-- ============================================================
-- Settings (key-value store for admin config)
-- ============================================================
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO settings (key, value) VALUES
    ('llm_provider',               '"openai"'),
    ('llm_model',                  '"gpt-5.6-luna"'),
    ('llm_temperature',            '0.7'),
    ('llm_max_tokens',             '500'),
    ('fallback_provider',          '"anthropic"'),
    ('fallback_model',             '"claude-haiku-4-5"'),
    ('auto_fallback',              'true'),
    ('ai_enabled',                 'true'),
    ('ai_orchestration_mode',      '"legacy"'),
    ('automatic_escalation_timeout_minutes', '180'),
    ('catalog_refresh_minutes',    '15'),
    ('catalog_pdf_interval_hours', '24'),
    ('max_conversation_history',   '20'),
    ('escalation_telegram_enabled','true'),
    ('broadcast_check_interval_minutes', '1'),
    ('token_reminder_hour',        '3'),
    ('token_reminder_minute',      '0'),
    ('daily_analytics_hour',       '1'),
    ('daily_analytics_minute',     '0'),
    ('exchange_rate_reference',    '"usd_bcv"'),
    ('manual_exchange_rate',       '""'),
    ('exchange_rate_usd_bcv',      '""'),
    ('exchange_rate_usd_bcv_effective_at', '""'),
    ('exchange_rate_usd_bcv_fetched_at', '""'),
    ('exchange_rate_usd_bcv_source', '""'),
    ('exchange_rate_eur_bcv',      '""'),
    ('exchange_rate_eur_bcv_effective_at', '""'),
    ('exchange_rate_eur_bcv_fetched_at', '""'),
    ('exchange_rate_eur_bcv_source', '""'),
    ('exchange_rate_usdt_binance', '""'),
    ('exchange_rate_usdt_binance_effective_at', '""'),
    ('exchange_rate_usdt_binance_fetched_at', '""'),
    ('exchange_rate_usdt_binance_source', '""'),
    ('exchange_rates_last_synced_at', '""'),
    ('store_phone_number',        '""'),
    ('order_discount_percent',     '10.0'),
    ('order_discount_threshold_usd','350.0'),
    ('kommo_strip_emoji',          'false'),
    ('kommo_emoji_mode_whatsapp',  '"safe"'),
    ('kommo_emoji_mode_instagram', '"safe"'),
    ('payment_methods',            '[]')
ON CONFLICT (key) DO NOTHING;

DO $$
DECLARE
    legacy_exchange_rate JSONB;
BEGIN
    SELECT value INTO legacy_exchange_rate
    FROM settings
    WHERE key = 'accepted_exchange_rate';

    IF legacy_exchange_rate IS NOT NULL AND BTRIM(legacy_exchange_rate #>> '{}') <> '' THEN
        INSERT INTO settings (key, value)
        VALUES ('manual_exchange_rate', legacy_exchange_rate)
        ON CONFLICT (key) DO UPDATE
        SET value = CASE
            WHEN BTRIM(settings.value #>> '{}') = '' THEN EXCLUDED.value
            ELSE settings.value
        END,
        updated_at = CASE
            WHEN BTRIM(settings.value #>> '{}') = '' THEN NOW()
            ELSE settings.updated_at
        END;

        INSERT INTO settings (key, value)
        VALUES ('exchange_rate_reference', '"manual"')
        ON CONFLICT (key) DO UPDATE
        SET value = CASE
            WHEN BTRIM(settings.value #>> '{}') IN ('', 'usd_bcv') THEN EXCLUDED.value
            ELSE settings.value
        END,
        updated_at = CASE
            WHEN BTRIM(settings.value #>> '{}') IN ('', 'usd_bcv') THEN NOW()
            ELSE settings.updated_at
        END;
    END IF;
END $$;

-- ============================================================
-- Usage tracking (for cost monitoring)
-- ============================================================
CREATE TABLE IF NOT EXISTS usage_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    customer_id     UUID REFERENCES customers(id) ON DELETE SET NULL,
    response_time_ms INTEGER,
    was_fallback    BOOLEAN DEFAULT FALSE,
    had_tool_calls  BOOLEAN DEFAULT FALSE,
    channel         TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_log(created_at DESC);

-- ============================================================
-- AI run observability (routing/agent metadata, no raw content)
-- ============================================================
CREATE TABLE IF NOT EXISTS ai_run_logs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id          UUID REFERENCES customers(id) ON DELETE SET NULL,
    channel              TEXT,
    orchestration_mode   TEXT NOT NULL DEFAULT 'legacy',
    selected_agent       TEXT NOT NULL DEFAULT 'legacy',
    route_intent         TEXT,
    route_source         TEXT,
    route_confidence     NUMERIC(4,3),
    provider             TEXT,
    model                TEXT,
    input_tokens         INTEGER NOT NULL DEFAULT 0,
    output_tokens        INTEGER NOT NULL DEFAULT 0,
    response_time_ms     INTEGER,
    tool_names           JSONB NOT NULL DEFAULT '[]'::jsonb,
    tool_rounds          INTEGER NOT NULL DEFAULT 0,
    handoff_occurred     BOOLEAN NOT NULL DEFAULT FALSE,
    fallback_occurred    BOOLEAN NOT NULL DEFAULT FALSE,
    escalation_occurred  BOOLEAN NOT NULL DEFAULT FALSE,
    shadow_evaluation    BOOLEAN NOT NULL DEFAULT FALSE,
    legacy_fallback      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at           TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_run_logs_created ON ai_run_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_run_logs_route ON ai_run_logs(orchestration_mode, selected_agent, route_source);
CREATE INDEX IF NOT EXISTS idx_ai_run_logs_customer ON ai_run_logs(customer_id, created_at DESC);

-- ============================================================
-- Daily analytics aggregates (computed by a scheduled job)
-- ============================================================
CREATE TABLE IF NOT EXISTS daily_analytics (
    date                DATE NOT NULL,
    channel             TEXT NOT NULL,           -- "whatsapp", "instagram", "all"
    provider            TEXT NOT NULL,           -- "openai", "anthropic", "all"
    total_messages_in   INTEGER DEFAULT 0,
    total_messages_out  INTEGER DEFAULT 0,
    unique_customers    INTEGER DEFAULT 0,
    new_customers       INTEGER DEFAULT 0,
    avg_response_ms     INTEGER DEFAULT 0,
    p95_response_ms     INTEGER DEFAULT 0,
    total_orders        INTEGER DEFAULT 0,
    total_revenue       NUMERIC(10,2) DEFAULT 0,
    orders_confirmed    INTEGER DEFAULT 0,
    escalations         INTEGER DEFAULT 0,
    total_input_tokens  BIGINT DEFAULT 0,
    total_output_tokens BIGINT DEFAULT 0,
    estimated_cost_usd  NUMERIC(10,4) DEFAULT 0,
    PRIMARY KEY (date, channel, provider)
);

CREATE INDEX IF NOT EXISTS idx_daily_analytics_date ON daily_analytics(date DESC);

-- ============================================================
-- Product popularity tracking
-- ============================================================
CREATE TABLE IF NOT EXISTS product_analytics (
    date         DATE NOT NULL,
    sku          TEXT NOT NULL,
    product_name TEXT,
    times_asked  INTEGER DEFAULT 0,
    times_ordered INTEGER DEFAULT 0,
    revenue      NUMERIC(10,2) DEFAULT 0,
    PRIMARY KEY (date, sku)
);

CREATE INDEX IF NOT EXISTS idx_product_analytics_date ON product_analytics(date DESC);

-- ============================================================
-- Kommo customer/channel mappings
-- ============================================================
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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT customer_channel_mappings_any_external_id CHECK (
        external_contact_id IS NOT NULL
        OR external_lead_id IS NOT NULL
        OR external_chat_id IS NOT NULL
        OR external_talk_id IS NOT NULL
        OR external_author_id IS NOT NULL
    )
);

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

DROP INDEX IF EXISTS uq_customer_channel_mappings_contact;
CREATE INDEX IF NOT EXISTS idx_customer_channel_mappings_contact
    ON customer_channel_mappings(provider, channel, external_contact_id, updated_at DESC)
    WHERE external_contact_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_customer_channel_mappings_lead
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
CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_meta_instagram_sender
    ON customer_channel_mappings(provider, channel, external_author_id)
    WHERE provider = 'meta' AND channel = 'instagram' AND external_author_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_meta_instagram_identity
    ON customer_channel_mappings(customer_id, provider, channel)
    WHERE provider = 'meta' AND channel = 'instagram' AND external_author_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_customer_channel_mappings_customer
    ON customer_channel_mappings(customer_id, provider, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_customer_channel_mappings_lookup
    ON customer_channel_mappings(provider, channel, updated_at DESC);

DROP TRIGGER IF EXISTS trg_customer_channel_mappings_updated_at ON customer_channel_mappings;
CREATE TRIGGER trg_customer_channel_mappings_updated_at
    BEFORE UPDATE ON customer_channel_mappings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- Durable Meta inbound queue
-- ============================================================
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
    processing_heartbeat_at TIMESTAMPTZ,
    processing_lease_token TEXT,
    outbound_started_at TIMESTAMPTZ,
    outbound_message_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    completed_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_meta_inbound_jobs_pending_sender
    ON meta_inbound_jobs(channel, sender_id) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS uq_meta_inbound_jobs_processing_sender
    ON meta_inbound_jobs(channel, sender_id) WHERE status = 'processing';
CREATE INDEX IF NOT EXISTS idx_meta_inbound_jobs_due
    ON meta_inbound_jobs(status, available_at, created_at);

CREATE TABLE IF NOT EXISTS meta_inbound_receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel TEXT NOT NULL CHECK (channel IN ('whatsapp', 'instagram')),
    external_message_id TEXT NOT NULL,
    job_id UUID NOT NULL REFERENCES meta_inbound_jobs(id) ON DELETE CASCADE,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(channel, external_message_id)
);

CREATE INDEX IF NOT EXISTS idx_meta_inbound_receipts_job
    ON meta_inbound_receipts(job_id);

-- ============================================================
-- Kommo durable message jobs
-- ============================================================
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
    instagram_content_context JSONB NOT NULL DEFAULT '{}'::jsonb,
    suppress_after_context BOOLEAN NOT NULL DEFAULT FALSE,
    automation_block_reason TEXT,
    salesbot_token_jti TEXT,
    salesbot_account_id TEXT,
    salesbot_user_id TEXT,
    salesbot_client_uuid TEXT,
    continuation_payload JSONB,
    continuation_response JSONB,
    assistant_message_persisted_at TIMESTAMPTZ
);

ALTER TABLE kommo_message_jobs
    ADD COLUMN IF NOT EXISTS author_id TEXT,
    ADD COLUMN IF NOT EXISTS author_name TEXT,
    ADD COLUMN IF NOT EXISTS author_username TEXT,
    ADD COLUMN IF NOT EXISTS author_profile_url TEXT,
    ADD COLUMN IF NOT EXISTS sender_username TEXT,
    ADD COLUMN IF NOT EXISTS sender_profile_url TEXT,
    ADD COLUMN IF NOT EXISTS interaction_type TEXT NOT NULL DEFAULT 'private_message',
    ADD COLUMN IF NOT EXISTS processing_lease_id UUID,
    ADD COLUMN IF NOT EXISTS ai_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS callback_claims JSONB,
    ADD COLUMN IF NOT EXISTS public_comment_context JSONB,
    ADD COLUMN IF NOT EXISTS instagram_content_context JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS suppress_after_context BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS automation_block_reason TEXT,
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
            'pending',
            'prepared',
            'waiting_for_salesbot',
            'waiting_for_context',
            'ready',
            'processing',
            'continuing',
            'sent',
            'discarded',
            'delivery_unknown',
            'failed'
        )
    );

ALTER TABLE kommo_message_jobs
    DROP CONSTRAINT IF EXISTS kommo_message_jobs_interaction_type_check;

ALTER TABLE kommo_message_jobs
    ADD CONSTRAINT kommo_message_jobs_interaction_type_check CHECK (
        interaction_type IN ('private_message', 'instagram_comment')
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
    WHERE status IN ('prepared', 'waiting_for_salesbot', 'waiting_for_context', 'ready', 'processing', 'continuing');
CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_message_jobs_pending_correlation
    ON kommo_message_jobs(correlation_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_kommo_message_jobs_delivery_unknown
    ON kommo_message_jobs(status, updated_at DESC)
    WHERE status = 'delivery_unknown';
CREATE INDEX IF NOT EXISTS idx_kommo_message_jobs_salesbot_token_jti
    ON kommo_message_jobs(salesbot_token_jti)
    WHERE salesbot_token_jti IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kommo_message_jobs_author
    ON kommo_message_jobs(author_id, updated_at DESC)
    WHERE author_id IS NOT NULL;

DROP TRIGGER IF EXISTS trg_kommo_message_jobs_updated_at ON kommo_message_jobs;
CREATE TRIGGER trg_kommo_message_jobs_updated_at
    BEFORE UPDATE ON kommo_message_jobs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE kommo_message_jobs IS 'Durable Kommo inbound jobs. Failed jobs are safe to retry only after confirming no sent job exists for the same external_message_id/correlation_id.';
COMMENT ON COLUMN kommo_message_jobs.continuation_payload IS 'Last Salesbot continuation payload attempted for this job.';
COMMENT ON COLUMN kommo_message_jobs.continuation_response IS 'Sanitized Salesbot continuation response when Kommo accepted the request.';
COMMENT ON COLUMN kommo_message_jobs.assistant_message_persisted_at IS 'Set after the delivered Kommo continuation has been persisted as assistant conversation history. Used to make retries idempotent.';
COMMENT ON COLUMN kommo_message_jobs.interaction_type IS 'private_message for WhatsApp/Instagram DMs, instagram_comment for public Instagram comment replies through Kommo native comment-triggered Salesbot callbacks.';
COMMENT ON COLUMN kommo_message_jobs.suppress_after_context IS 'When true, Story context is applied after callback but AI execution remains suppressed.';
COMMENT ON COLUMN kommo_message_jobs.automation_block_reason IS 'Durable pre-Salesbot automation block reason used after Story context correlation.';

-- ============================================================
-- Kommo outbound delivery state (transport details, not history)
-- ============================================================
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
CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_outbound_deliveries_job_transport_media
    ON kommo_outbound_deliveries(job_id, transport, COALESCE(media_type, ''));
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

COMMENT ON TABLE kommo_outbound_deliveries IS 'Transport-specific outbound delivery state. Semantic media history belongs in conversations.attachments.';
COMMENT ON COLUMN kommo_outbound_deliveries.attachment_metadata IS 'Transport/provider attachment metadata; never include this JSON in LLM conversation history.';

-- ============================================================
-- Kommo inbound message receipts
-- ============================================================
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
    message_text TEXT,
    normalized_text_hash TEXT,
    received_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT kommo_message_receipts_status_check CHECK (receipt_status IN ('created', 'merged'))
);

ALTER TABLE kommo_message_receipts
    ADD COLUMN IF NOT EXISTS author_id TEXT,
    ADD COLUMN IF NOT EXISTS interaction_type TEXT NOT NULL DEFAULT 'private_message',
    ADD COLUMN IF NOT EXISTS message_text TEXT,
    ADD COLUMN IF NOT EXISTS normalized_text_hash TEXT;

ALTER TABLE kommo_message_receipts
    DROP CONSTRAINT IF EXISTS kommo_message_receipts_interaction_type_check;

ALTER TABLE kommo_message_receipts
    ADD CONSTRAINT kommo_message_receipts_interaction_type_check CHECK (
        interaction_type IN ('private_message', 'instagram_comment')
    );

CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_message_receipts_external_message
    ON kommo_message_receipts(external_message_id);
CREATE INDEX IF NOT EXISTS idx_kommo_message_receipts_job
    ON kommo_message_receipts(job_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kommo_receipts_story_text
    ON kommo_message_receipts(job_id, normalized_text_hash, received_at, created_at)
    WHERE channel = 'instagram'
      AND interaction_type = 'private_message'
      AND normalized_text_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kommo_message_receipts_correlation
    ON kommo_message_receipts(correlation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kommo_message_receipts_author
    ON kommo_message_receipts(author_id, created_at DESC)
    WHERE author_id IS NOT NULL;

INSERT INTO kommo_message_receipts (
    external_message_id, job_id, correlation_id, lead_id, contact_id, chat_id,
    talk_id, author_id, origin, channel, interaction_type, receipt_status, received_at, created_at
)
SELECT
    external_message_id,
    id,
    correlation_id,
    lead_id,
    contact_id,
    chat_id,
    talk_id,
    author_id,
    origin,
    channel,
    COALESCE(interaction_type, 'private_message'),
    CASE WHEN status = 'discarded' AND COALESCE(last_error, '') LIKE 'Merged into %' THEN 'merged' ELSE 'created' END,
    created_at,
    created_at
FROM kommo_message_jobs
WHERE external_message_id IS NOT NULL
ON CONFLICT (external_message_id) DO NOTHING;

COMMENT ON TABLE kommo_message_receipts IS 'Durable Kommo inbound message receipts keyed by external_message_id. Rapid messages can merge into one buffered job without creating fake discarded jobs.';
COMMENT ON COLUMN kommo_message_receipts.receipt_status IS 'created when the receipt opened a new job, merged when it was appended to an existing buffered job.';

-- ============================================================
-- Database privilege hardening
-- ============================================================
-- The application connects directly as the database owner. No store table is
-- intended for direct browser access, so public roles receive no policies or
-- object privileges.
ALTER TABLE customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversation_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE broadcasts ENABLE ROW LEVEL SECURITY;
ALTER TABLE broadcast_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_run_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE daily_analytics ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_analytics ENABLE ROW LEVEL SECURITY;
ALTER TABLE customer_channel_mappings ENABLE ROW LEVEL SECURITY;
ALTER TABLE meta_inbound_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE meta_inbound_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE kommo_message_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE kommo_outbound_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE kommo_message_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- PostgreSQL grants EXECUTE on new functions to PUBLIC by default. Remove that
-- default for future objects.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

REVOKE EXECUTE ON FUNCTION public.set_updated_at() FROM PUBLIC;

CREATE TABLE IF NOT EXISTS payment_proof_replays (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proof_hash TEXT UNIQUE,
    reference_key TEXT UNIQUE,
    original_order_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (proof_hash IS NOT NULL OR reference_key IS NOT NULL)
);
ALTER TABLE payment_proof_replays ENABLE ROW LEVEL SECURITY;
REVOKE ALL PRIVILEGES ON payment_proof_replays FROM PUBLIC;

INSERT INTO schema_migrations (version, name) VALUES
    (1, 'fresh_install_baseline')
ON CONFLICT (version) DO NOTHING;

COMMIT;
