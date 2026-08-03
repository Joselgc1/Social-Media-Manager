-- Preserve pre-Salesbot automation suppression through Meta Story correlation.
BEGIN;

ALTER TABLE kommo_message_jobs
    ADD COLUMN IF NOT EXISTS suppress_after_context BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS automation_block_reason TEXT;

COMMENT ON COLUMN kommo_message_jobs.suppress_after_context IS
    'When true, Story context is applied after callback but AI execution remains suppressed.';
COMMENT ON COLUMN kommo_message_jobs.automation_block_reason IS
    'Durable pre-Salesbot automation block reason used after Story context correlation.';

INSERT INTO schema_migrations (version, name) VALUES
    (8, 'kommo_story_deferred_suppression')
ON CONFLICT (version) DO NOTHING;

COMMIT;
