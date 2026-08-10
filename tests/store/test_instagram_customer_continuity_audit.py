import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _script_module():
    path = ROOT / "store/scripts/audit_instagram_customer_continuity.py"
    spec = importlib.util.spec_from_file_location("audit_instagram_customer_continuity", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Connection:
    def __init__(self, row):
        self.row = row
        self.execute = AsyncMock()
        self.fetchrow = AsyncMock(return_value=row)
        self.close = AsyncMock()
        self.transaction_calls = []

    def transaction(self, **kwargs):
        self.transaction_calls.append(kwargs)
        return _Transaction()


@pytest.mark.asyncio
async def test_audit_is_read_only_time_bounded_and_outputs_counts_only():
    script = _script_module()
    row = {
        "instagram_kommo_count": 2,
        "meta_igsid_count": 1,
        "history_without_meta_count": 1,
        "duplicate_count": 1,
    }
    connection = _Connection(row)

    report = await script.build_report(connection)

    assert connection.transaction_calls == [{"readonly": True}]
    connection.execute.assert_awaited_once_with("SET LOCAL statement_timeout = '5000ms'")
    connection.fetchrow.assert_awaited_once_with(script.AUDIT_QUERY)
    assert report["read_only"] is True
    assert report["shared_identifier_duplicate_candidates"] == 1
    serialized = str(report).lower()
    for forbidden in (
        "platform_id",
        "external_author_id",
        "customer_uuid",
        "message",
        "database_url",
        "postgresql://",
    ):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_audit_connects_once_and_always_closes_connection():
    script = _script_module()
    connection = _Connection({})
    connector = AsyncMock(return_value=connection)
    expected = {"read_only": True}
    build_report = AsyncMock(return_value=expected)
    script.build_report = build_report

    report = await script.run_audit(
        "postgresql://secret-host/private",
        connector=connector,
    )

    assert report == expected
    connector.assert_awaited_once_with("postgresql://secret-host/private", command_timeout=6)
    build_report.assert_awaited_once_with(connection)
    connection.close.assert_awaited_once()


def test_cli_failure_never_prints_database_url_or_exception(monkeypatch, capsys):
    script = _script_module()

    async def fail(*args, **kwargs):
        raise RuntimeError("postgresql://user:secret@private-host/store")

    monkeypatch.setattr(script, "run_audit", fail)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@private-host/store")
    monkeypatch.setattr(sys, "argv", ["audit_instagram_customer_continuity.py"])

    assert script.main() == 1
    captured = capsys.readouterr()
    assert "postgresql://" not in captured.err
    assert "secret" not in captured.err
    assert captured.out == ""
