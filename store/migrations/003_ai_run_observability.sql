-- AI run observability and orchestration rollout setting
-- Run after 002_conversation_sessions.sql

INSERT INTO settings (key, value)
VALUES ('ai_orchestration_mode', '"legacy"')
ON CONFLICT (key) DO NOTHING;

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
