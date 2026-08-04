-- Preserve ordered inbound audio attachments across Kommo's debounce window.
BEGIN;

ALTER TABLE kommo_message_jobs
    ADD COLUMN IF NOT EXISTS inbound_attachments JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN kommo_message_jobs.inbound_attachments IS
    'Ordered inbound audio/image metadata used for transcription and existing image analysis.';

INSERT INTO schema_migrations (version, name) VALUES
    (14, 'kommo_inbound_attachments')
ON CONFLICT (version) DO NOTHING;

COMMIT;
