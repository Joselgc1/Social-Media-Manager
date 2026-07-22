-- Add explicit marketing consent and durable, at-most-once broadcast delivery records.
BEGIN;

ALTER TABLE customers
    ADD COLUMN IF NOT EXISTS marketing_opt_in BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS marketing_opt_in_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS marketing_opt_out_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_customers_marketing_whatsapp
    ON customers(last_active DESC)
    WHERE channel = 'whatsapp' AND marketing_opt_in = TRUE;

ALTER TABLE broadcasts
    ADD COLUMN IF NOT EXISTS audience_seeded_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS broadcast_deliveries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    broadcast_id    UUID NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
    customer_id     UUID REFERENCES customers(id) ON DELETE SET NULL,
    platform_id     TEXT NOT NULL,
    channel         TEXT NOT NULL DEFAULT 'whatsapp' CHECK (channel = 'whatsapp'),
    display_name    TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    claimed_at      TIMESTAMPTZ,
    sent_at         TIMESTAMPTZ,
    failed_at       TIMESTAMPTZ,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (broadcast_id, channel, platform_id)
);

CREATE INDEX IF NOT EXISTS idx_broadcast_deliveries_claim
    ON broadcast_deliveries(broadcast_id, status, created_at);

ALTER TABLE broadcast_deliveries ENABLE ROW LEVEL SECURITY;
REVOKE ALL PRIVILEGES ON broadcast_deliveries FROM anon, authenticated;

INSERT INTO schema_migrations (version, name)
VALUES (3, 'broadcast_delivery_safety')
ON CONFLICT (version) DO NOTHING;

COMMIT;
