import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


def _runner_module():
    path = Path(__file__).resolve().parents[2] / "store/scripts/migrate.py"
    spec = importlib.util.spec_from_file_location("instagram_content_migration_runner", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_instagram_content_migration_is_idempotent_and_records_version_four():
    migration = Path("store/migrations/004_instagram_content_mapping.sql").read_text()

    assert "CREATE TABLE IF NOT EXISTS instagram_content" in migration
    assert "CREATE TABLE IF NOT EXISTS instagram_content_products" in migration
    assert migration.count("CREATE UNIQUE INDEX IF NOT EXISTS") == 3
    assert "CREATE INDEX IF NOT EXISTS idx_instagram_content_status_updated" in migration
    assert "DROP TRIGGER IF EXISTS trg_instagram_content_updated_at" in migration
    assert "ALTER TABLE instagram_content ENABLE ROW LEVEL SECURITY" in migration
    assert "ALTER TABLE instagram_content_products ENABLE ROW LEVEL SECURITY" in migration
    assert "REVOKE ALL PRIVILEGES ON instagram_content FROM PUBLIC" in migration
    assert "REVOKE ALL PRIVILEGES ON instagram_content_products FROM PUBLIC" in migration
    assert "ON CONFLICT (version) DO NOTHING" in migration
    assert "(4, 'instagram_content_mapping')" in migration


def test_instagram_content_can_be_identified_only_by_media_id():
    migration = Path("store/migrations/004_instagram_content_mapping.sql").read_text()

    assert "permalink TEXT," in migration
    assert "normalized_permalink TEXT," in migration
    assert "ALTER COLUMN permalink DROP NOT NULL" in migration
    assert "ALTER COLUMN normalized_permalink DROP NOT NULL" in migration
    assert "normalized_permalink IS NOT NULL" in migration
    assert "OR shortcode IS NOT NULL" in migration
    assert "OR media_id IS NOT NULL" in migration
    assert "DROP CONSTRAINT IF EXISTS instagram_content_stable_identifier_check" in migration


@pytest.mark.asyncio
async def test_migration_runner_can_apply_migration_four_repeatedly(monkeypatch):
    runner = _runner_module()
    connection = AsyncMock()
    monkeypatch.setattr(runner.asyncpg, "connect", AsyncMock(return_value=connection))

    await runner.run_migration("postgresql://test")
    await runner.run_migration("postgresql://test")

    migration_sql = runner.INSTAGRAM_CONTENT_MIGRATION_PATH.read_text(encoding="utf-8")
    calls = [call for call in connection.execute.await_args_list if call.args == (migration_sql,)]
    assert len(calls) == 2
