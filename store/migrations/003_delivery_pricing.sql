-- Add configurable prepaid delivery pricing without changing prior migrations.
BEGIN;

ALTER TABLE customers
    ADD COLUMN IF NOT EXISTS last_fulfillment_type TEXT,
    ADD COLUMN IF NOT EXISTS last_shipping_zone TEXT,
    ADD COLUMN IF NOT EXISTS last_pickup_agency TEXT;

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS fulfillment_type TEXT,
    ADD COLUMN IF NOT EXISTS shipping_zone TEXT,
    ADD COLUMN IF NOT EXISTS pickup_agency TEXT,
    ADD COLUMN IF NOT EXISTS merchandise_total NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS shipping_fee NUMERIC(10,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS shipping_currency TEXT NOT NULL DEFAULT 'USD';

UPDATE orders
SET merchandise_total = total
WHERE merchandise_total IS NULL;

INSERT INTO settings (key, value) VALUES
    ('shipping_policy',
     '{"currency":"USD","home_delivery_cities":[{"name":"Valencia","aliases":["Valencia, Carabobo"]},{"name":"Naguanagua","aliases":[]},{"name":"San Diego","aliases":["San Diego, Carabobo"]}],"home_delivery_zones":[],"courier_destination_rates":[]}')
ON CONFLICT (key) DO NOTHING;

INSERT INTO schema_migrations (version, name) VALUES
    (3, 'delivery_pricing')
ON CONFLICT (version) DO NOTHING;

COMMIT;
