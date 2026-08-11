from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "store/migrations/001_schema.sql"
MIGRATION = ROOT / "store/migrations/016_kommo_waiting_for_delivery.sql"


def test_waiting_for_delivery_state_is_in_baseline_and_upgrade():
    for path in (BASELINE, MIGRATION):
        schema = path.read_text(encoding="utf-8")
        assert "'waiting_for_delivery'" in schema
        assert "pending_assistant_message JSONB" in schema
        assert "delivery_reconcile_after_at TIMESTAMPTZ" in schema
        assert "delivery_reconcile_attempt_count INTEGER NOT NULL DEFAULT 0" in schema


def test_waiting_for_delivery_blocks_another_active_job():
    for path in (BASELINE, MIGRATION):
        schema = path.read_text(encoding="utf-8")
        active_index = schema.split("uq_kommo_message_jobs_active_salesbot", 1)[1]
        assert "'waiting_for_delivery'" in active_index


def test_reconciliation_migration_is_versioned_and_indexed():
    schema = MIGRATION.read_text(encoding="utf-8")
    assert "idx_kommo_message_jobs_delivery_reconcile" in schema
    assert "(16, 'kommo_waiting_for_delivery')" in schema
