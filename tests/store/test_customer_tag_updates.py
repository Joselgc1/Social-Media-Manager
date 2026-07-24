import json
from unittest.mock import AsyncMock

import pytest


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _DBHandle:
    def transaction(self):
        return _Tx()


@pytest.mark.asyncio
async def test_add_tags_locks_customer_row_before_merging(monkeypatch):
    from app.crm import customers

    fetch_one = AsyncMock(return_value={"tags": ["new_lead"]})
    execute = AsyncMock()
    monkeypatch.setattr(customers.db, "get_db", lambda: _DBHandle())
    monkeypatch.setattr(customers.db, "fetch_one", fetch_one)
    monkeypatch.setattr(customers.db, "execute", execute)

    await customers.add_tags("customer-1", ["vip"])

    assert "FOR UPDATE" in fetch_one.await_args.args[0]
    values = execute.await_args.args[1]
    assert json.loads(values["tags"]) == ["new_lead", "vip"]


@pytest.mark.asyncio
async def test_remove_tag_locks_customer_row_before_merging(monkeypatch):
    from app.crm import customers

    fetch_one = AsyncMock(return_value={"tags": ["new_lead", "vip"]})
    execute = AsyncMock()
    monkeypatch.setattr(customers.db, "get_db", lambda: _DBHandle())
    monkeypatch.setattr(customers.db, "fetch_one", fetch_one)
    monkeypatch.setattr(customers.db, "execute", execute)

    await customers.remove_tag("customer-1", "vip")

    assert "FOR UPDATE" in fetch_one.await_args.args[0]
    values = execute.await_args.args[1]
    assert json.loads(values["tags"]) == ["new_lead"]
