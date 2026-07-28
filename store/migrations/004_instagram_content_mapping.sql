-- Add administrative Instagram content-to-product mappings.
BEGIN;

CREATE TABLE IF NOT EXISTS instagram_content (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content_type TEXT NOT NULL CHECK (content_type IN ('post', 'carousel', 'reel', 'story')),
    permalink TEXT,
    normalized_permalink TEXT,
    shortcode TEXT,
    media_id TEXT,
    caption_snapshot TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE instagram_content
    ALTER COLUMN permalink DROP NOT NULL,
    ALTER COLUMN normalized_permalink DROP NOT NULL;

ALTER TABLE instagram_content
    DROP CONSTRAINT IF EXISTS instagram_content_stable_identifier_check;
ALTER TABLE instagram_content
    ADD CONSTRAINT instagram_content_stable_identifier_check CHECK (
        normalized_permalink IS NOT NULL
        OR shortcode IS NOT NULL
        OR media_id IS NOT NULL
    );

CREATE TABLE IF NOT EXISTS instagram_content_products (
    content_id UUID NOT NULL REFERENCES instagram_content(id) ON DELETE CASCADE,
    product_sku TEXT NOT NULL,
    display_order INTEGER NOT NULL DEFAULT 0 CHECK (display_order >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (content_id, product_sku)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_instagram_content_normalized_permalink
    ON instagram_content(normalized_permalink)
    WHERE normalized_permalink IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_instagram_content_shortcode
    ON instagram_content(shortcode)
    WHERE shortcode IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_instagram_content_media_id
    ON instagram_content(media_id)
    WHERE media_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_instagram_content_status_updated
    ON instagram_content(status, updated_at DESC);

DROP TRIGGER IF EXISTS trg_instagram_content_updated_at ON instagram_content;
CREATE TRIGGER trg_instagram_content_updated_at
    BEFORE UPDATE ON instagram_content
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE instagram_content ENABLE ROW LEVEL SECURITY;
ALTER TABLE instagram_content_products ENABLE ROW LEVEL SECURITY;
REVOKE ALL PRIVILEGES ON instagram_content FROM PUBLIC;
REVOKE ALL PRIVILEGES ON instagram_content_products FROM PUBLIC;

INSERT INTO schema_migrations (version, name) VALUES
    (4, 'instagram_content_mapping')
ON CONFLICT (version) DO NOTHING;

COMMIT;
