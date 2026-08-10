-- Retire live Instagram/Kommo processing without dropping historical correlation data.
BEGIN;

UPDATE kommo_message_jobs
SET status = 'discarded',
    last_error = 'instagram_transport_retired',
    processing_lease_id = NULL,
    completed_at = COALESCE(completed_at, NOW()),
    updated_at = NOW()
WHERE channel = 'instagram'
  AND status IN ('pending', 'prepared')
  AND salesbot_launched_at IS NULL
  AND return_url IS NULL
  AND ai_started_at IS NULL;

-- A SQL migration cannot safely complete an external Salesbot continuation.
-- Keep the old deployment serving until launched or uncertain work drains.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM kommo_message_jobs
        WHERE channel = 'instagram'
          AND status IN (
              'pending', 'prepared', 'waiting_for_salesbot', 'waiting_for_context',
              'ready', 'processing', 'continuing'
          )
    ) THEN
        RAISE EXCEPTION
            'Instagram Kommo jobs are still active; disable Instagram Kommo ingress and retry after they drain';
    END IF;
END $$;

UPDATE meta_instagram_context_events
SET correlation_status = 'expired',
    correlation_details = correlation_details || jsonb_build_object(
        'deprecated_reason', 'instagram_transport_is_meta_native'
    ),
    updated_at = NOW()
WHERE correlation_status IN ('pending', 'ambiguous');

DELETE FROM settings WHERE key = 'kommo_emoji_mode_instagram';

INSERT INTO schema_migrations (version, name) VALUES
    (18, 'retire_kommo_instagram')
ON CONFLICT (version) DO NOTHING;

COMMIT;
