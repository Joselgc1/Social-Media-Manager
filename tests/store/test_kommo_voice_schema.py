from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_voice_migration_and_fresh_schema_add_message_type():
    migration = (ROOT / "store/migrations/013_kommo_inbound_voice.sql").read_text(encoding="utf-8")
    schema = (ROOT / "store/migrations/001_schema.sql").read_text(encoding="utf-8")
    runner = (ROOT / "store/scripts/migrate.py").read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS message_type TEXT" in migration
    assert "(13, 'kommo_inbound_voice')" in migration
    assert "message_type TEXT" in schema
    assert '"013_kommo_inbound_voice.sql"' in runner


def test_inbound_attachments_migration_is_versioned_and_in_fresh_schema():
    migration = (ROOT / "store/migrations/014_kommo_inbound_attachments.sql").read_text(
        encoding="utf-8"
    )
    schema = (ROOT / "store/migrations/001_schema.sql").read_text(encoding="utf-8")
    runner = (ROOT / "store/scripts/migrate.py").read_text(encoding="utf-8")

    column = "inbound_attachments JSONB NOT NULL DEFAULT '[]'::jsonb"
    assert f"ADD COLUMN IF NOT EXISTS {column}" in migration
    assert "(14, 'kommo_inbound_attachments')" in migration
    assert column in schema
    assert '"014_kommo_inbound_attachments.sql"' in runner
