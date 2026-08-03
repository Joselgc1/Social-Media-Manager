from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (ROOT / "store/migrations/006_instagram_story_context.sql").read_text(encoding="utf-8")
PRODUCTION_FIXES = (ROOT / "store/migrations/007_instagram_story_production_fixes.sql").read_text(
    encoding="utf-8"
)
DEFERRED_SUPPRESSION = (
    ROOT / "store/migrations/008_kommo_story_deferred_suppression.sql"
).read_text(encoding="utf-8")
BASELINE = (ROOT / "store/migrations/001_schema.sql").read_text(encoding="utf-8")
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


def test_story_production_fixes_add_receipt_text_hash_and_filtered_index():
    for schema in (PRODUCTION_FIXES, BASELINE):
        assert "message_text TEXT" in schema
        assert "normalized_text_hash TEXT" in schema
        assert "idx_kommo_receipts_story_text" in schema
        assert "interaction_type = 'private_message'" in schema
        assert "normalized_text_hash IS NOT NULL" in schema


def test_story_production_fixes_enforce_unique_meta_sender_and_customer_identity():
    for schema in (PRODUCTION_FIXES, BASELINE):
        assert "uq_customer_meta_instagram_sender" in schema
        assert "ON customer_channel_mappings(provider, channel, external_author_id)" in schema
        assert "uq_customer_meta_instagram_identity" in schema
        assert "ON customer_channel_mappings(customer_id, provider, channel)" in schema
        assert "provider = 'meta'" in schema
        assert "channel = 'instagram'" in schema
    assert "(7, 'instagram_story_production_fixes')" in PRODUCTION_FIXES


def test_story_production_fixes_are_registered_after_story_context_migration():
    assert '"007_instagram_story_production_fixes.sql"' in RUNNER
    assert RUNNER.index("INSTAGRAM_STORY_CONTEXT_MIGRATION_PATH.read_text") < RUNNER.index(
        "INSTAGRAM_STORY_PRODUCTION_FIXES_MIGRATION_PATH.read_text"
    )


def test_deferred_story_suppression_has_dedicated_durable_columns():
    for schema in (DEFERRED_SUPPRESSION, BASELINE):
        assert "suppress_after_context BOOLEAN NOT NULL DEFAULT FALSE" in schema
        assert "automation_block_reason TEXT" in schema
    assert "(8, 'kommo_story_deferred_suppression')" in DEFERRED_SUPPRESSION


def test_deferred_story_suppression_migration_runs_last():
    assert '"008_kommo_story_deferred_suppression.sql"' in RUNNER
    assert RUNNER.index("INSTAGRAM_STORY_PRODUCTION_FIXES_MIGRATION_PATH.read_text") < RUNNER.index(
        "KOMMO_STORY_DEFERRED_SUPPRESSION_MIGRATION_PATH.read_text"
    )
