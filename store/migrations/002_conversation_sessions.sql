-- Persistent workflow state for multi-agent conversation orchestration
-- Run after 001_schema.sql

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
