from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = (ROOT / "store/migrations/001_schema.sql").read_text(encoding="utf-8")
MIGRATION_009 = (ROOT / "store/migrations/009_kommo_media_history_foundation.sql").read_text(
    encoding="utf-8"
)
MIGRATION_010 = (ROOT / "store/migrations/010_kommo_outbound_delivery_uniqueness.sql").read_text(
    encoding="utf-8"
)
RUNNER = (ROOT / "store/scripts/migrate.py").read_text(encoding="utf-8")


def test_phase2_migration_adds_semantic_conversation_attachments():
    for schema in (BASELINE, MIGRATION_009):
        assert "attachments JSONB" in schema
    assert "Kommo Drive UUIDs" in MIGRATION_009


def test_phase2_migration_adds_outbound_delivery_foundation():
    for schema in (BASELINE, MIGRATION_009):
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
        assert "uq_kommo_outbound_deliveries_job_transport_media" not in schema
        assert "uq_kommo_outbound_deliveries_salesbot_job" in schema
        assert "uq_kommo_outbound_deliveries_provider_message" in schema
        assert "uq_kommo_outbound_deliveries_request" in schema
        assert "ALTER TABLE kommo_outbound_deliveries ENABLE ROW LEVEL SECURITY" in schema
    assert "REVOKE ALL PRIVILEGES ON kommo_outbound_deliveries FROM PUBLIC" in MIGRATION_009


def test_phase2_migration_is_versioned_and_runs_after_phase1_schema():
    assert "(9, 'kommo_media_history_foundation')" in MIGRATION_009
    assert '"009_kommo_media_history_foundation.sql"' in RUNNER
    assert RUNNER.index("KOMMO_STORY_DEFERRED_SUPPRESSION_MIGRATION_PATH.read_text") < RUNNER.index(
        "KOMMO_MEDIA_HISTORY_FOUNDATION_MIGRATION_PATH.read_text"
    )


def test_phase2_hardening_migration_drops_overly_broad_media_uniqueness():
    assert "DROP INDEX IF EXISTS uq_kommo_outbound_deliveries_job_transport_media" in MIGRATION_010
    assert "uq_kommo_outbound_deliveries_job_transport_media" not in BASELINE
    assert "(10, 'kommo_outbound_delivery_uniqueness')" in MIGRATION_010
    assert '"010_kommo_outbound_delivery_uniqueness.sql"' in RUNNER
    assert RUNNER.index("KOMMO_MEDIA_HISTORY_FOUNDATION_MIGRATION_PATH.read_text") < RUNNER.index(
        "KOMMO_OUTBOUND_DELIVERY_UNIQUENESS_MIGRATION_PATH.read_text"
    )


def test_two_chats_api_deliveries_with_same_media_type_can_have_distinct_requests():
    for schema in (BASELINE, MIGRATION_009):
        assert "UNIQUE INDEX IF NOT EXISTS uq_kommo_outbound_deliveries_request" in schema
        assert "ON kommo_outbound_deliveries(job_id, transport, request_fingerprint)" in schema
        assert "COALESCE(media_type, '')" not in schema


def test_duplicate_request_and_provider_ids_remain_constrained():
    for schema in (BASELINE, MIGRATION_009, MIGRATION_010):
        assert "uq_kommo_outbound_deliveries_request" in schema
        assert "WHERE request_fingerprint IS NOT NULL" in schema
        assert "uq_kommo_outbound_deliveries_provider_message" in schema
        assert "WHERE provider_message_id IS NOT NULL" in schema


def test_salesbot_remains_one_delivery_per_job_without_limiting_chats_api():
    for schema in (BASELINE, MIGRATION_009, MIGRATION_010):
        assert "ON kommo_outbound_deliveries(job_id)" in schema
        assert "WHERE transport = 'salesbot'" in schema
        assert "WHERE transport = 'chats_api'" not in schema
