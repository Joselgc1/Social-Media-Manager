-- Upgrade master databases created from an older 001_master_schema.sql.
-- Do not run 001_master_schema.sql again on an existing database.
BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

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

ALTER TABLE stores ENABLE ROW LEVEL SECURITY;
ALTER TABLE store_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE master_audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE exchange_rates ENABLE ROW LEVEL SECURITY;
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

INSERT INTO schema_migrations (version, name) VALUES
    (1, 'legacy_unversioned_baseline'),
    (2, 'versioned_schema_and_security_hardening')
ON CONFLICT (version) DO NOTHING;

COMMIT;
