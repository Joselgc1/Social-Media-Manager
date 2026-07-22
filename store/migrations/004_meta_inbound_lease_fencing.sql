-- Fence Meta inbound processing leases and persist outbound delivery evidence.
BEGIN;

ALTER TABLE meta_inbound_jobs
    ADD COLUMN IF NOT EXISTS processing_heartbeat_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS processing_lease_token TEXT,
    ADD COLUMN IF NOT EXISTS outbound_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS outbound_message_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

INSERT INTO schema_migrations (version, name)
VALUES (4, 'meta_inbound_lease_fencing')
ON CONFLICT (version) DO NOTHING;

COMMIT;
