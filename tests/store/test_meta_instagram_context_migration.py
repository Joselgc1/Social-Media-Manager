from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "store"
    / "migrations"
    / "005_meta_instagram_context.sql"
).read_text(encoding="utf-8")
BASELINE = (
    Path(__file__).resolve().parents[2] / "store" / "migrations" / "001_schema.sql"
).read_text(encoding="utf-8")


def test_meta_context_migration_has_deduplication_and_one_to_one_constraints():
    assert "CREATE TABLE IF NOT EXISTS meta_instagram_context_events" in MIGRATION
    assert "uq_meta_instagram_context_external_event" in MIGRATION
    assert "uq_meta_instagram_context_comment" in MIGRATION
    assert "uq_meta_instagram_context_message" in MIGRATION
    assert "uq_meta_instagram_context_matched_job" in MIGRATION
    assert "uq_kommo_message_jobs_meta_context_event" in MIGRATION
    assert "ENABLE ROW LEVEL SECURITY" in MIGRATION
    assert "REVOKE ALL PRIVILEGES ON meta_instagram_context_events FROM PUBLIC" in MIGRATION


def test_meta_context_migration_adds_waiting_and_context_status_constraints():
    assert "'waiting_for_context'" in MIGRATION
    for status in ("not_required", "pending", "matched", "ambiguous", "timed_out"):
        assert f"'{status}'" in MIGRATION
    for status in ("pending", "matched", "ambiguous", "expired"):
        assert f"'{status}'" in MIGRATION
    assert "context_deadline_at" in MIGRATION
    assert "context_correlation_score" in MIGRATION
    assert "(5, 'meta_instagram_context')" in MIGRATION


def test_reapplied_baseline_accepts_waiting_context_rows_before_migration_five_runs():
    assert "'waiting_for_context'" in BASELINE
    active_index = BASELINE.split("CREATE UNIQUE INDEX IF NOT EXISTS uq_kommo_message_jobs_active_salesbot", 1)[1]
    assert "'waiting_for_context'" in active_index
