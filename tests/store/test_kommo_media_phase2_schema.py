from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = (ROOT / "store/migrations/001_schema.sql").read_text(encoding="utf-8")
MIGRATION = (ROOT / "store/migrations/009_kommo_media_history_foundation.sql").read_text(
    encoding="utf-8"
)
RUNNER = (ROOT / "store/scripts/migrate.py").read_text(encoding="utf-8")


def test_phase2_migration_adds_semantic_conversation_attachments():
    for schema in (BASELINE, MIGRATION):
        assert "attachments JSONB" in schema
    assert "Kommo Drive UUIDs" in MIGRATION


def test_phase2_migration_adds_outbound_delivery_foundation():
    for schema in (BASELINE, MIGRATION):
        assert "CREATE TABLE IF NOT EXISTS kommo_outbound_deliveries" in schema
        assert "job_id UUID NOT NULL REFERENCES kommo_message_jobs(id) ON DELETE CASCADE" in schema
        assert "transport IN ('salesbot', 'chats_api')" in schema
        assert "media_type TEXT" in schema
        assert "provider_message_id TEXT" in schema
        assert "request_fingerprint TEXT" in schema
        assert "attachment_metadata JSONB NOT NULL DEFAULT '{}'::jsonb" in schema
        assert "accepted_at TIMESTAMPTZ" in schema
        assert "confirmed_at TIMESTAMPTZ" in schema
        assert "last_error TEXT" in schema
        assert "uq_kommo_outbound_deliveries_job_transport_media" in schema
        assert "uq_kommo_outbound_deliveries_provider_message" in schema
        assert "uq_kommo_outbound_deliveries_request" in schema
        assert "ALTER TABLE kommo_outbound_deliveries ENABLE ROW LEVEL SECURITY" in schema
    assert "REVOKE ALL PRIVILEGES ON kommo_outbound_deliveries FROM PUBLIC" in MIGRATION


def test_phase2_migration_is_versioned_and_runs_after_phase1_schema():
    assert "(9, 'kommo_media_history_foundation')" in MIGRATION
    assert '"009_kommo_media_history_foundation.sql"' in RUNNER
    assert RUNNER.index("KOMMO_STORY_DEFERRED_SUPPRESSION_MIGRATION_PATH.read_text") < RUNNER.index(
        "KOMMO_MEDIA_HISTORY_FOUNDATION_MIGRATION_PATH.read_text"
    )
