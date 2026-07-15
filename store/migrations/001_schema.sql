-- Full store schema
-- Consolidated base schema for fresh installs (includes launch hardening changes)
-- Run this against your Supabase PostgreSQL instance

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

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
    is_blocked            BOOLEAN DEFAULT FALSE,
    last_shipping_address TEXT,
    last_shipping_city    TEXT,
    last_shipping_method  TEXT,
    UNIQUE(channel, platform_id)
);

CREATE INDEX IF NOT EXISTS idx_customers_tags ON customers USING gin(tags);
CREATE INDEX IF NOT EXISTS idx_customers_last_active ON customers(last_active DESC);

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
    ('catalog_refresh_minutes',    '15'),
    ('catalog_pdf_interval_hours', '24'),
    ('max_conversation_history',   '20'),
    ('escalation_telegram_enabled','true'),
    ('broadcast_check_interval_minutes', '1'),
    ('token_reminder_hour',        '3'),
    ('token_reminder_minute',      '0'),
    ('daily_analytics_hour',       '1'),
    ('daily_analytics_minute',     '0'),
    ('accepted_exchange_rate',     '""'),
    ('order_discount_percent',     '10.0'),
    ('order_discount_threshold_usd','350.0'),
    ('payment_methods',            '[]');

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
