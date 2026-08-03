from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (ROOT / "store/migrations/006_instagram_story_context.sql").read_text(encoding="utf-8")
RUNNER = (ROOT / "store/scripts/migrate.py").read_text(encoding="utf-8")


def test_story_migration_adds_durable_mapping_session_and_job_context():
    assert "ADD COLUMN IF NOT EXISTS thumbnail_url TEXT" in MIGRATION
    assert "ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ" in MIGRATION
    assert "instagram_content_context JSONB NOT NULL DEFAULT '{}'::jsonb" in MIGRATION
    assert "instagram_context_expires_at TIMESTAMPTZ" in MIGRATION
    assert "idx_meta_story_reply_correlation" in MIGRATION
    assert "WHERE event_type = 'story_reply'" in MIGRATION
    assert "(6, 'instagram_story_context')" in MIGRATION


def test_story_migration_is_registered_by_runner():
    assert '"006_instagram_story_context.sql"' in RUNNER
