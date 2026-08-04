-- Preserve the normalized Kommo message type needed for inbound voice transcription.
BEGIN;

ALTER TABLE kommo_message_jobs
    ADD COLUMN IF NOT EXISTS message_type TEXT;

COMMENT ON COLUMN kommo_message_jobs.message_type IS
    'Normalized incoming Kommo message type; voice and audio jobs are transcribed before AI processing.';

INSERT INTO schema_migrations (version, name) VALUES
    (13, 'kommo_inbound_voice')
ON CONFLICT (version) DO NOTHING;

COMMIT;
