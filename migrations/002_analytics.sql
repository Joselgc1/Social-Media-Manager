-- Migration 002: Analytics, A/B testing, and response time tracking
-- Run this after 001_initial.sql

-- Add response time tracking to usage_log
ALTER TABLE usage_log ADD COLUMN IF NOT EXISTS response_time_ms INTEGER;
ALTER TABLE usage_log ADD COLUMN IF NOT EXISTS was_fallback BOOLEAN DEFAULT FALSE;
ALTER TABLE usage_log ADD COLUMN IF NOT EXISTS had_tool_calls BOOLEAN DEFAULT FALSE;
ALTER TABLE usage_log ADD COLUMN IF NOT EXISTS channel TEXT;

-- Add A/B test group to customers
ALTER TABLE customers ADD COLUMN IF NOT EXISTS ab_provider TEXT;  -- "openai" or "anthropic" or NULL

-- Daily analytics aggregates (computed by a scheduled job)
CREATE TABLE IF NOT EXISTS daily_analytics (
    date            DATE NOT NULL,
    channel         TEXT NOT NULL,           -- "whatsapp", "instagram", "all"
    provider        TEXT NOT NULL,           -- "openai", "anthropic", "all"

    -- Volume
    total_messages_in   INTEGER DEFAULT 0,
    total_messages_out  INTEGER DEFAULT 0,
    unique_customers    INTEGER DEFAULT 0,
    new_customers       INTEGER DEFAULT 0,

    -- Performance
    avg_response_ms     INTEGER DEFAULT 0,   -- Average LLM response time
    p95_response_ms     INTEGER DEFAULT 0,   -- 95th percentile response time

    -- Conversion
    total_orders        INTEGER DEFAULT 0,
    total_revenue       NUMERIC(10,2) DEFAULT 0,
    orders_confirmed    INTEGER DEFAULT 0,

    -- Escalation
    escalations         INTEGER DEFAULT 0,

    -- Cost
    total_input_tokens  BIGINT DEFAULT 0,
    total_output_tokens BIGINT DEFAULT 0,
    estimated_cost_usd  NUMERIC(10,4) DEFAULT 0,

    PRIMARY KEY (date, channel, provider)
);

-- Product popularity tracking
CREATE TABLE IF NOT EXISTS product_analytics (
    date            DATE NOT NULL,
    sku             TEXT NOT NULL,
    product_name    TEXT,
    times_asked     INTEGER DEFAULT 0,       -- How many times check_inventory was called for this
    times_ordered   INTEGER DEFAULT 0,       -- How many times it appeared in an order
    revenue         NUMERIC(10,2) DEFAULT 0,
    PRIMARY KEY (date, sku)
);

CREATE INDEX IF NOT EXISTS idx_daily_analytics_date ON daily_analytics(date DESC);
CREATE INDEX IF NOT EXISTS idx_product_analytics_date ON product_analytics(date DESC);

-- A/B test setting default
INSERT INTO settings (key, value) VALUES ('ab_test_enabled', 'false')
ON CONFLICT (key) DO NOTHING;
