-- Kommo debounce receipts and transport formatting setting.
-- Run after 003_kommo_hardening.sql against each store database.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

DO $$
BEGIN
    IF to_regclass('public.kommo_message_jobs') IS NULL THEN
        RAISE EXCEPTION 'kommo_message_jobs is missing. Run store/migrations/002_kommo_integration.sql and store/migrations/003_kommo_hardening.sql first.';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.kommo_message_receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_message_id TEXT NOT NULL,
    job_id UUID REFERENCES public.kommo_message_jobs(id) ON DELETE SET NULL,
    correlation_id TEXT NOT NULL,
    lead_id TEXT,
    contact_id TEXT,
    chat_id TEXT,
    talk_id TEXT,
    origin TEXT,
    channel TEXT,
    receipt_status TEXT NOT NULL DEFAULT 'created',
    received_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT kommo_message_receipts_status_check CHECK (receipt_status IN ('created', 'merged'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_message_receipts_external_message
    ON public.kommo_message_receipts(external_message_id);
CREATE INDEX IF NOT EXISTS idx_kommo_message_receipts_job
    ON public.kommo_message_receipts(job_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kommo_message_receipts_correlation
    ON public.kommo_message_receipts(correlation_id, created_at DESC);

INSERT INTO public.kommo_message_receipts (
    external_message_id, job_id, correlation_id, lead_id, contact_id, chat_id,
    talk_id, origin, channel, receipt_status, received_at, created_at
)
SELECT
    external_message_id,
    id,
    correlation_id,
    lead_id,
    contact_id,
    chat_id,
    talk_id,
    origin,
    channel,
    CASE WHEN status = 'discarded' AND COALESCE(last_error, '') LIKE 'Merged into %' THEN 'merged' ELSE 'created' END,
    created_at,
    created_at
FROM public.kommo_message_jobs
WHERE external_message_id IS NOT NULL
ON CONFLICT (external_message_id) DO NOTHING;

INSERT INTO public.settings (key, value)
VALUES ('kommo_strip_emoji', 'false')
ON CONFLICT (key) DO NOTHING;

COMMENT ON TABLE public.kommo_message_receipts IS 'Durable Kommo inbound message receipts keyed by external_message_id. Rapid messages can merge into one buffered job without creating fake discarded jobs.';
COMMENT ON COLUMN public.kommo_message_receipts.receipt_status IS 'created when the receipt opened a new job, merged when it was appended to an existing buffered job.';
