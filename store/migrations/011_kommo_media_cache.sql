-- Cache Kommo Drive uploads independently from semantic conversation history.
BEGIN;

CREATE TABLE IF NOT EXISTS kommo_media_cache (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    media_type TEXT NOT NULL CHECK (media_type IN ('product_image', 'catalog_pdf')),
    cache_key TEXT NOT NULL,
    drive_uuid UUID NOT NULL,
    drive_version_uuid UUID NOT NULL,
    file_name TEXT NOT NULL,
    mime_type TEXT NOT NULL CHECK (
        mime_type IN ('image/jpeg', 'image/png', 'image/webp', 'application/pdf')
    ),
    file_size BIGINT NOT NULL CHECK (file_size > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (media_type, cache_key)
);

DROP TRIGGER IF EXISTS trg_kommo_media_cache_updated_at ON kommo_media_cache;
CREATE TRIGGER trg_kommo_media_cache_updated_at
    BEFORE UPDATE ON kommo_media_cache
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON TABLE kommo_media_cache IS
    'Reusable Kommo Drive upload identifiers keyed by semantic media content. This table is transport state, not LLM history.';

ALTER TABLE kommo_media_cache ENABLE ROW LEVEL SECURITY;
REVOKE ALL PRIVILEGES ON kommo_media_cache FROM PUBLIC;

INSERT INTO schema_migrations (version, name) VALUES
    (11, 'kommo_media_cache')
ON CONFLICT (version) DO NOTHING;

COMMIT;
