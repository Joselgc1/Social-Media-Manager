-- Upgrade any pre-consolidation master database to the current schema baseline.
BEGIN;

CREATE TABLE IF NOT EXISTS exchange_rates (
    rate_key TEXT PRIMARY KEY,
    currency_code TEXT NOT NULL,
    market TEXT NOT NULL,
    rate NUMERIC(18, 8) NOT NULL,
    effective_at TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    previous_rate NUMERIC(18, 8),
    change_percentage NUMERIC(12, 6),
    source TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_exchange_rates_fetched_at ON exchange_rates(fetched_at DESC);
ALTER TABLE exchange_rates ENABLE ROW LEVEL SECURITY;
REVOKE ALL PRIVILEGES ON exchange_rates FROM anon, authenticated;

DELETE FROM schema_migrations WHERE version > 1;
INSERT INTO schema_migrations (version, name)
VALUES (2, 'consolidated_upgrade')
ON CONFLICT (version) DO NOTHING;

COMMIT;
