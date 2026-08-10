from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (ROOT / "store/migrations/015_conversation_interaction_scope.sql").read_text(
    encoding="utf-8"
)
BASELINE = (ROOT / "store/migrations/001_schema.sql").read_text(encoding="utf-8")
RUNNER = (ROOT / "store/scripts/migrate.py").read_text(encoding="utf-8")
BASELINE_CONVERSATIONS = BASELINE.split("-- Conversations (message history)", 1)[1].split(
    "-- Orders", 1
)[0]


def test_conversation_scope_migration_adds_and_backfills_interaction_type():
    assert "ADD COLUMN IF NOT EXISTS interaction_type" in MIGRATION
    assert "DEFAULT 'private_message'" in MIGRATION
    assert "conversation.source_id = 'kommo-job:' || job.id::text" in MIGRATION
    assert "SET interaction_type = job.interaction_type" in MIGRATION
    assert "job.interaction_type IN ('private_message', 'instagram_comment')" in MIGRATION
    assert "(15, 'conversation_interaction_scope')" in MIGRATION


def test_baseline_excludes_conversation_interaction_scope():
    assert "interaction_type" not in BASELINE_CONVERSATIONS
    assert "conversations_interaction_type_check" not in BASELINE_CONVERSATIONS
    assert "idx_conv_customer_interaction" not in BASELINE_CONVERSATIONS


def test_migration_015_owns_conversation_interaction_scope_and_runner_includes_it():
    assert "ADD COLUMN IF NOT EXISTS interaction_type" in MIGRATION
    assert "conversations_interaction_type_check" in MIGRATION
    assert "idx_conv_customer_interaction" in MIGRATION
    assert "015_conversation_interaction_scope.sql" in RUNNER
    assert "await connection.execute(conversation_interaction_scope_migration_sql)" in RUNNER
