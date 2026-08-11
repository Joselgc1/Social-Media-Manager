-- Reconcile accepted direct Instagram messages before persisting assistant history.
BEGIN;

ALTER TABLE kommo_message_jobs
    ADD COLUMN IF NOT EXISTS pending_assistant_message JSONB,
    ADD COLUMN IF NOT EXISTS delivery_wait_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS delivery_reconcile_after_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS delivery_reconcile_attempt_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE kommo_message_jobs
    DROP CONSTRAINT IF EXISTS kommo_message_jobs_status_check;

ALTER TABLE kommo_message_jobs
    ADD CONSTRAINT kommo_message_jobs_status_check CHECK (
        status IN (
            'pending',
            'prepared',
            'waiting_for_salesbot',
            'waiting_for_context',
            'waiting_for_delivery',
            'ready',
            'processing',
            'continuing',
            'sent',
            'discarded',
            'delivery_unknown',
            'failed'
        )
    );

DROP INDEX IF EXISTS uq_kommo_message_jobs_active_salesbot;
CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_message_jobs_active_salesbot
    ON kommo_message_jobs(correlation_id)
    WHERE status IN (
        'prepared', 'waiting_for_salesbot', 'waiting_for_context',
        'waiting_for_delivery', 'ready', 'processing', 'continuing'
    );

CREATE INDEX IF NOT EXISTS idx_kommo_message_jobs_delivery_reconcile
    ON kommo_message_jobs(delivery_reconcile_after_at, created_at)
    WHERE status = 'waiting_for_delivery';

COMMENT ON COLUMN kommo_message_jobs.pending_assistant_message IS
    'Assistant history payload held until direct Instagram delivery is confirmed.';
COMMENT ON COLUMN kommo_message_jobs.delivery_wait_started_at IS
    'Time when a direct Instagram Chats API message was accepted for asynchronous delivery.';

INSERT INTO schema_migrations (version, name) VALUES
    (16, 'kommo_waiting_for_delivery')
ON CONFLICT (version) DO NOTHING;

COMMIT;
