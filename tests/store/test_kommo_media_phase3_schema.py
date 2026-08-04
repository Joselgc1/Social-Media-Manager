from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = (ROOT / "store/migrations/001_schema.sql").read_text(encoding="utf-8")
MIGRATION_011 = (ROOT / "store/migrations/011_kommo_media_cache.sql").read_text(encoding="utf-8")
MIGRATION_012 = (ROOT / "store/migrations/012_kommo_media_content_hash.sql").read_text(
    encoding="utf-8"
)
RUNNER = (ROOT / "store/scripts/migrate.py").read_text(encoding="utf-8")


def test_phase3_cache_schema_is_versioned_and_in_fresh_schema():
    for schema in (BASELINE, MIGRATION_011):
        assert "CREATE TABLE IF NOT EXISTS kommo_media_cache" in schema
        assert "media_type TEXT NOT NULL" in schema
        assert "cache_key TEXT NOT NULL" in schema
        assert "drive_uuid UUID NOT NULL" in schema
        assert "drive_version_uuid UUID NOT NULL" in schema
        assert "file_name TEXT NOT NULL" in schema
        assert "mime_type TEXT NOT NULL" in schema
        assert "file_size BIGINT NOT NULL" in schema
        assert "ALTER TABLE kommo_media_cache ENABLE ROW LEVEL SECURITY" in schema
    assert "(11, 'kommo_media_cache')" in MIGRATION_011
    assert '"011_kommo_media_cache.sql"' in RUNNER


def test_phase35_cache_adds_content_identity_and_replaces_url_only_uniqueness():
    assert "content_hash TEXT NOT NULL" in BASELINE
    assert "UNIQUE (media_type, cache_key, content_hash)" in BASELINE
    assert "ADD COLUMN content_hash TEXT" in MIGRATION_012
    assert "TRUNCATE TABLE kommo_media_cache" in MIGRATION_012
    assert "UNIQUE (media_type, cache_key, content_hash)" in MIGRATION_012
    assert "(12, 'kommo_media_content_hash')" in MIGRATION_012
    assert '"012_kommo_media_content_hash.sql"' in RUNNER


def test_media_cache_remains_separate_from_semantic_history():
    for migration in (MIGRATION_011, MIGRATION_012):
        assert "conversations" not in migration
        assert "access_token" not in migration
        assert "raw" not in migration.lower()
