from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = (ROOT / "store/migrations/001_schema.sql").read_text(encoding="utf-8")
MIGRATION = (ROOT / "store/migrations/011_kommo_media_cache.sql").read_text(encoding="utf-8")
RUNNER = (ROOT / "store/scripts/migrate.py").read_text(encoding="utf-8")


def test_phase3_cache_schema_is_versioned_and_in_fresh_schema():
    for schema in (BASELINE, MIGRATION):
        assert "CREATE TABLE IF NOT EXISTS kommo_media_cache" in schema
        assert "media_type TEXT NOT NULL" in schema
        assert "cache_key TEXT NOT NULL" in schema
        assert "drive_uuid UUID NOT NULL" in schema
        assert "drive_version_uuid UUID NOT NULL" in schema
        assert "file_name TEXT NOT NULL" in schema
        assert "mime_type TEXT NOT NULL" in schema
        assert "file_size BIGINT NOT NULL" in schema
        assert "UNIQUE (media_type, cache_key)" in schema
        assert "ALTER TABLE kommo_media_cache ENABLE ROW LEVEL SECURITY" in schema
    assert "(11, 'kommo_media_cache')" in MIGRATION
    assert '"011_kommo_media_cache.sql"' in RUNNER


def test_media_cache_remains_separate_from_semantic_history():
    assert "conversations" not in MIGRATION
    assert "access_token" not in MIGRATION
    assert "raw" not in MIGRATION.lower()
