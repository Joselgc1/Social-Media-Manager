-- Full store schema
-- Run this against your Supabase PostgreSQL instance

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================
-- Customers
-- ============================================================
CREATE TABLE customers (
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
    ab_provider           TEXT,                        -- "openai" or "anthropic" or NULL (A/B test group)
    last_shipping_address TEXT,
    last_shipping_city    TEXT,
    last_shipping_method  TEXT,
    UNIQUE(channel, platform_id)
);

CREATE INDEX idx_customers_tags ON customers USING gin(tags);
CREATE INDEX idx_customers_last_active ON customers(last_active DESC);

-- ============================================================
-- Conversations (message history)
-- ============================================================
CREATE TABLE conversations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id     UUID REFERENCES customers(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,               -- "user" or "assistant"
    content         TEXT NOT NULL,
    channel         TEXT NOT NULL,
    media_url       TEXT,
    function_calls  JSONB,                       -- Log of any tools the AI invoked
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_conv_customer ON conversations(customer_id, created_at DESC);
CREATE INDEX idx_conv_created ON conversations(created_at DESC);

-- ============================================================
-- Orders
-- ============================================================
CREATE TABLE orders (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id      UUID REFERENCES customers(id) ON DELETE SET NULL,
    items            JSONB NOT NULL,              -- [{"name": "...", "sku": "...", "size": "M", "qty": 1, "price": 28}]
    total            NUMERIC(10,2) NOT NULL,
    payment_method   TEXT,                        -- "zelle", "binance", "zinli", "bolivares"
    payment_status   TEXT DEFAULT 'pending',      -- "pending", "proof_received", "confirmed", "failed"
    payment_proof    TEXT,                        -- URL to payment screenshot
    shipping_method  TEXT,                        -- "mrw" or "zoom"
    shipping_city    TEXT,
    shipping_address TEXT,
    shipping_status  TEXT DEFAULT 'pending',      -- "pending", "shipped", "delivered"
    tracking_number  TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_orders_customer ON orders(customer_id, created_at DESC);
CREATE INDEX idx_orders_created ON orders(created_at DESC);
CREATE INDEX idx_orders_status ON orders(payment_status, shipping_status);

-- ============================================================
-- Broadcasts
-- ============================================================
CREATE TABLE broadcasts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    template_name   TEXT NOT NULL,
    template_params JSONB,
    target_tags     JSONB NOT NULL,              -- ["vip", "interested:pajamas"]
    target_channel  TEXT DEFAULT 'whatsapp',
    scheduled_at    TIMESTAMPTZ,
    sent_at         TIMESTAMPTZ,
    recipients      INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'draft'         -- "draft", "scheduled", "sending", "sent", "failed"
);

-- ============================================================
-- Settings (key-value store for admin config)
-- ============================================================
CREATE TABLE settings (
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
    ('catalog_refresh_minutes',    '15'),
    ('max_conversation_history',   '20'),
    ('escalation_telegram_enabled','true'),
    ('ab_test_enabled',            'false');

-- ============================================================
-- Usage tracking (for cost monitoring)
-- ============================================================
CREATE TABLE usage_log (
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

CREATE INDEX idx_usage_created ON usage_log(created_at DESC);

-- ============================================================
-- Daily analytics aggregates (computed by a scheduled job)
-- ============================================================
CREATE TABLE daily_analytics (
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

CREATE INDEX idx_daily_analytics_date ON daily_analytics(date DESC);

-- ============================================================
-- Product popularity tracking
-- ============================================================
CREATE TABLE product_analytics (
    date         DATE NOT NULL,
    sku          TEXT NOT NULL,
    product_name TEXT,
    times_asked  INTEGER DEFAULT 0,
    times_ordered INTEGER DEFAULT 0,
    revenue      NUMERIC(10,2) DEFAULT 0,
    PRIMARY KEY (date, sku)
);

CREATE INDEX idx_product_analytics_date ON product_analytics(date DESC);
