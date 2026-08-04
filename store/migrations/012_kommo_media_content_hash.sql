-- Make Kommo media cache entries content-aware and discard unverifiable URL-only rows.
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'kommo_media_cache'
          AND column_name = 'content_hash'
    ) THEN
        TRUNCATE TABLE kommo_media_cache;
        ALTER TABLE kommo_media_cache ADD COLUMN content_hash TEXT;
        ALTER TABLE kommo_media_cache ALTER COLUMN content_hash SET NOT NULL;
    END IF;
END $$;

ALTER TABLE kommo_media_cache
    DROP CONSTRAINT IF EXISTS kommo_media_cache_media_type_cache_key_key;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'kommo_media_cache_identity_content_key'
          AND conrelid = 'public.kommo_media_cache'::regclass
    ) THEN
        ALTER TABLE kommo_media_cache
            ADD CONSTRAINT kommo_media_cache_identity_content_key
            UNIQUE (media_type, cache_key, content_hash);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'kommo_media_cache_content_hash_check'
          AND conrelid = 'public.kommo_media_cache'::regclass
    ) THEN
        ALTER TABLE kommo_media_cache
            ADD CONSTRAINT kommo_media_cache_content_hash_check
            CHECK (content_hash ~ '^[0-9a-f]{64}$');
    END IF;
END $$;

COMMENT ON COLUMN kommo_media_cache.cache_key IS
    'Deterministic source identity: normalized image URL hash or catalog fingerprint.';
COMMENT ON COLUMN kommo_media_cache.content_hash IS
    'SHA-256 of the validated bytes uploaded to Kommo Drive.';

INSERT INTO schema_migrations (version, name) VALUES
    (12, 'kommo_media_content_hash')
ON CONFLICT (version) DO NOTHING;

COMMIT;
