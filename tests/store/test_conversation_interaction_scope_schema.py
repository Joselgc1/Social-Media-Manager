from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (ROOT / "store/migrations/015_conversation_interaction_scope.sql").read_text(
    encoding="utf-8"
)
BASELINE = (ROOT / "store/migrations/001_schema.sql").read_text(encoding="utf-8")
RUNNER = (ROOT / "store/scripts/migrate.py").read_text(encoding="utf-8")


def test_conversation_scope_migration_adds_and_backfills_interaction_type():
    assert "ADD COLUMN IF NOT EXISTS interaction_type" in MIGRATION
    assert "DEFAULT 'private_message'" in MIGRATION
    assert "conversation.source_id = 'kommo-job:' || job.id::text" in MIGRATION
    assert "SET interaction_type = job.interaction_type" in MIGRATION
    assert "job.interaction_type IN ('private_message', 'instagram_comment')" in MIGRATION
    assert "(15, 'conversation_interaction_scope')" in MIGRATION


def test_fresh_schema_and_runner_include_conversation_scope():
    assert "interaction_type TEXT NOT NULL DEFAULT 'private_message'" in BASELINE
    assert "conversations_interaction_type_check" in BASELINE
    assert "idx_conv_customer_interaction" in BASELINE
    assert "015_conversation_interaction_scope.sql" in RUNNER
