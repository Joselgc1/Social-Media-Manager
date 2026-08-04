-- Allow multiple Chats API media deliveries per job while preserving idempotency.
BEGIN;

DROP INDEX IF EXISTS uq_kommo_outbound_deliveries_job_transport_media;

CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_outbound_deliveries_salesbot_job
    ON kommo_outbound_deliveries(job_id)
    WHERE transport = 'salesbot';

CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_outbound_deliveries_provider_message
    ON kommo_outbound_deliveries(transport, provider_message_id)
    WHERE provider_message_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_outbound_deliveries_request
    ON kommo_outbound_deliveries(job_id, transport, request_fingerprint)
    WHERE request_fingerprint IS NOT NULL;

INSERT INTO schema_migrations (version, name) VALUES
    (10, 'kommo_outbound_delivery_uniqueness')
ON CONFLICT (version) DO NOTHING;

COMMIT;
