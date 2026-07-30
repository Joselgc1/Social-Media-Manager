import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


def _runner_module():
    path = Path(__file__).resolve().parents[2] / "store/scripts/migrate.py"
    spec = importlib.util.spec_from_file_location("store_migration_runner", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_store_migration_runner_executes_schema_and_closes_connection(monkeypatch):
    runner = _runner_module()
    connection = AsyncMock()
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr(runner.asyncpg, "connect", connect)

    await runner.run_migration("postgresql://user:secret@localhost:5432/store")

    migration_sql = runner.MIGRATION_PATH.read_text(encoding="utf-8")
    delivery_migration_sql = runner.DELIVERY_MIGRATION_PATH.read_text(encoding="utf-8")
    instagram_content_migration_sql = runner.INSTAGRAM_CONTENT_MIGRATION_PATH.read_text(encoding="utf-8")
    meta_instagram_context_migration_sql = runner.META_INSTAGRAM_CONTEXT_MIGRATION_PATH.read_text(
        encoding="utf-8"
    )
    assert connection.execute.await_args_list[1].args == (migration_sql,)
    assert connection.execute.await_args_list[2].args == (delivery_migration_sql,)
    assert connection.execute.await_args_list[3].args == (instagram_content_migration_sql,)
    assert connection.execute.await_args_list[4].args == (meta_instagram_context_migration_sql,)
    assert connection.execute.await_args_list[0].args[0] == "SELECT pg_advisory_lock($1)"
    assert connection.execute.await_args_list[5].args[0] == "SELECT pg_advisory_unlock($1)"
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_store_migration_runner_releases_lock_and_closes_on_failure(monkeypatch):
    runner = _runner_module()
    connection = AsyncMock()

    async def execute(query, *args):
        if query == runner.MIGRATION_PATH.read_text(encoding="utf-8"):
            raise RuntimeError("migration failed")

    connection.execute.side_effect = execute
    monkeypatch.setattr(runner.asyncpg, "connect", AsyncMock(return_value=connection))

    with pytest.raises(RuntimeError, match="migration failed"):
        await runner.run_migration("postgresql://user:secret@localhost:5432/store")

    assert connection.execute.await_args_list[-1].args[0] == "SELECT pg_advisory_unlock($1)"
    connection.close.assert_awaited_once()


def test_store_migration_runner_failure_logs_no_database_url(monkeypatch, caplog):
    runner = _runner_module()
    database_url = "postgresql://user:secret@localhost:5432/store"
    monkeypatch.setattr(runner, "run_migration", AsyncMock(side_effect=RuntimeError(database_url)))

    assert runner.main() == 1
    assert database_url not in caplog.text
    assert "Store database migration failed" in caplog.text
