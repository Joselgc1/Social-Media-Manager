-- Master Control Plane — Database Schema
-- Run this in the master Supabase project's SQL Editor

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Store registry
CREATE TABLE IF NOT EXISTS stores (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    owner_name      TEXT DEFAULT '',
    owner_contact   TEXT DEFAULT '',
    app_url         TEXT DEFAULT '',
    railway_service_id TEXT DEFAULT '',
    railway_project_id TEXT DEFAULT '',
    db_url_encrypted TEXT NOT NULL,
    status          TEXT DEFAULT 'active',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    last_seen       TIMESTAMPTZ
);

-- Store credentials (each row = one env var for one store, encrypted)
CREATE TABLE IF NOT EXISTS store_credentials (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id        UUID NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    key             TEXT NOT NULL,
    value_encrypted TEXT NOT NULL,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(store_id, key)
);

-- Audit log
CREATE TABLE IF NOT EXISTS master_audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action          TEXT NOT NULL,
    store_id        UUID REFERENCES stores(id) ON DELETE SET NULL,
    detail          TEXT DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Latest normalized Venezuela exchange rates fetched centrally from DolarVZLA
CREATE TABLE IF NOT EXISTS exchange_rates (
    rate_key          TEXT PRIMARY KEY,
    currency_code     TEXT NOT NULL,
    market            TEXT NOT NULL,
    rate              NUMERIC(18, 8) NOT NULL,
    effective_at      TIMESTAMPTZ NOT NULL,
    fetched_at        TIMESTAMPTZ NOT NULL,
    previous_rate     NUMERIC(18, 8),
    change_percentage NUMERIC(12, 6),
    source            TEXT NOT NULL,
    updated_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(rate_key)
);

-- Index for faster credential lookups
CREATE INDEX IF NOT EXISTS idx_store_credentials_store_id ON store_credentials(store_id);

-- Index for audit log queries
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON master_audit_log(created_at DESC);

-- Index for exchange-rate freshness checks
CREATE INDEX IF NOT EXISTS idx_exchange_rates_fetched_at ON exchange_rates(fetched_at DESC);

-- Supabase Data API lockdown. The master service uses a direct PostgreSQL
-- connection; none of these control-plane records are public API tables.
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
    (1, 'fresh_install_baseline'),
    (2, 'versioned_schema_and_security_hardening')
ON CONFLICT (version) DO NOTHING;

COMMIT;
