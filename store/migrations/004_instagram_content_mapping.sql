-- Add administrative Instagram content-to-product mappings.
BEGIN;

CREATE TABLE IF NOT EXISTS instagram_content (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content_type TEXT NOT NULL CHECK (content_type IN ('post', 'carousel', 'reel', 'story')),
    permalink TEXT NOT NULL,
    normalized_permalink TEXT NOT NULL,
    shortcode TEXT,
    media_id TEXT,
    caption_snapshot TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
