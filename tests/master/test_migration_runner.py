import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


def _runner_module():
    path = Path(__file__).resolve().parents[2] / "master/scripts/migrate.py"
    spec = importlib.util.spec_from_file_location("master_migration_runner", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_master_migration_runner_executes_schema_and_closes_connection(monkeypatch):
    runner = _runner_module()
    connection = AsyncMock()
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr(runner.asyncpg, "connect", connect)

    await runner.run_migration("postgresql://user:secret@localhost:5432/master")

    migration_sql = runner.MIGRATION_PATH.read_text(encoding="utf-8")
    assert connection.execute.await_args_list[1].args == (migration_sql,)
    assert connection.execute.await_args_list[0].args[0] == "SELECT pg_advisory_lock($1)"
    assert connection.execute.await_args_list[2].args[0] == "SELECT pg_advisory_unlock($1)"
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_master_migration_runner_releases_lock_and_closes_on_failure(monkeypatch):
    runner = _runner_module()
    connection = AsyncMock()

    async def execute(query, *args):
        if query == runner.MIGRATION_PATH.read_text(encoding="utf-8"):
            raise RuntimeError("migration failed")

    connection.execute.side_effect = execute
    monkeypatch.setattr(runner.asyncpg, "connect", AsyncMock(return_value=connection))

    with pytest.raises(RuntimeError, match="migration failed"):
        await runner.run_migration("postgresql://user:secret@localhost:5432/master")

    assert connection.execute.await_args_list[-1].args[0] == "SELECT pg_advisory_unlock($1)"
    connection.close.assert_awaited_once()


def test_master_migration_runner_failure_logs_no_database_url(monkeypatch, caplog):
    runner = _runner_module()
    database_url = "postgresql://user:secret@localhost:5432/master"
    monkeypatch.setattr(runner, "run_migration", AsyncMock(side_effect=RuntimeError(database_url)))

    assert runner.main() == 1
    assert database_url not in caplog.text
    assert "Master database migration failed" in caplog.text
