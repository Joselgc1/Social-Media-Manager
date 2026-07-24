-- Master Control Plane — Database Schema
-- Run this in the master Supabase project's SQL Editor

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
