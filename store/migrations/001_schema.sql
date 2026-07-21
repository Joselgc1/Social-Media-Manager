-- Full store schema
-- Single consolidated schema for fresh installs, including multi-agent workflow state,
-- AI run observability, Kommo integration tables, and hardening.
-- Run this against your Supabase PostgreSQL instance

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

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
    function_calls  JSONB,                       -- Log of any tools the AI invoked
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conv_customer ON conversations(customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conv_created ON conversations(created_at DESC);

-- ============================================================
-- Orders
-- ============================================================
CREATE TABLE IF NOT EXISTS orders (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id      UUID REFERENCES customers(id) ON DELETE SET NULL,
    items            JSONB NOT NULL,              -- [{"name": "...", "sku": "...", "size": "M", "qty": 1, "price": 28}]
    total            NUMERIC(10,2) NOT NULL,
    payment_method   TEXT,                        -- Store-defined payment method name
    payment_status   TEXT DEFAULT 'pending',      -- "pending", "proof_received", "confirmed", "failed"
    payment_proof    TEXT,                        -- URL to payment screenshot
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
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversation_sessions_active_agent
    ON conversation_sessions(active_agent);

CREATE INDEX IF NOT EXISTS idx_conversation_sessions_updated
    ON conversation_sessions(updated_at DESC);

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
    recipients      INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'draft'         -- "draft", "scheduled", "sending", "sent", "partial", "failed"
);

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
    ('llm_model',                  '"gpt-5.4-nano"'),
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
CREATE INDEX IF NOT EXISTS idx_customer_channel_mappings_lookup
    ON customer_channel_mappings(provider, channel, updated_at DESC);

DROP TRIGGER IF EXISTS trg_customer_channel_mappings_updated_at ON customer_channel_mappings;
CREATE TRIGGER trg_customer_channel_mappings_updated_at
    BEFORE UPDATE ON customer_channel_mappings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

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
    ADD COLUMN IF NOT EXISTS author_id TEXT,
    ADD COLUMN IF NOT EXISTS author_name TEXT,
    ADD COLUMN IF NOT EXISTS interaction_type TEXT NOT NULL DEFAULT 'private_message',
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
    WHERE status IN ('prepared', 'waiting_for_salesbot', 'ready', 'processing', 'continuing');
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
    received_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT kommo_message_receipts_status_check CHECK (receipt_status IN ('created', 'merged'))
);

ALTER TABLE kommo_message_receipts
    ADD COLUMN IF NOT EXISTS author_id TEXT,
    ADD COLUMN IF NOT EXISTS interaction_type TEXT NOT NULL DEFAULT 'private_message';

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
