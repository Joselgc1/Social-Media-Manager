from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError


def _database():
    database = MagicMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    database.transaction.return_value = transaction
    return database


@pytest.mark.asyncio
async def test_update_store_encrypts_database_url_and_disconnects_old_pool(monkeypatch):
    from app.stores import api
    from app.stores.models import StoreUpdate

    execute = AsyncMock()
    disconnect = AsyncMock()
    audit = AsyncMock()
    monkeypatch.setattr(
        api.db,
        "fetch_one",
        AsyncMock(return_value={"id": "store-1", "db_url_encrypted": "old-ciphertext"}),
    )
    monkeypatch.setattr(api.db, "execute", execute)
    monkeypatch.setattr(api.db, "get_db", lambda: _database())
    monkeypatch.setattr(api, "encrypt", lambda value: "new-ciphertext")
    monkeypatch.setattr(api, "decrypt", lambda value: "postgresql://old:secret@db/old")
    monkeypatch.setattr(api, "_disconnect_store_pool", disconnect)
    monkeypatch.setattr(api, "_audit", audit)

    result = await api.update_store(
        "store-1", StoreUpdate(db_url="postgresql://new:secret@db/new")
    )

    assert result == {"ok": True}
    assert any("db_url_encrypted" in call.args[0] for call in execute.await_args_list)
    assert all("new:secret" not in str(call) for call in execute.await_args_list)
    disconnect.assert_awaited_once_with("postgresql://old:secret@db/old")
    assert "new:secret" not in audit.await_args.args[2]
    assert "database URL" in audit.await_args.args[2]


@pytest.mark.asyncio
async def test_blank_or_omitted_database_url_preserves_current_value(monkeypatch):
    from app.stores import api
    from app.stores.models import StoreUpdate

    execute = AsyncMock()
    monkeypatch.setattr(
        api.db,
        "fetch_one",
        AsyncMock(return_value={"id": "store-1", "db_url_encrypted": "old-ciphertext"}),
    )
    monkeypatch.setattr(api.db, "execute", execute)
    monkeypatch.setattr(api.db, "get_db", lambda: _database())
    monkeypatch.setattr(api, "encrypt", MagicMock())
    monkeypatch.setattr(api, "_disconnect_store_pool", AsyncMock())
    monkeypatch.setattr(api, "_audit", AsyncMock())

    assert StoreUpdate(db_url="   ").db_url is None
    await api.update_store("store-1", StoreUpdate(name="Renamed"))

    api.encrypt.assert_not_called()
    assert all("db_url_encrypted" not in call.args[0] for call in execute.await_args_list)


@pytest.mark.parametrize(
    "database_url",
    ["mysql://user:secret@db/app", "https://db.example/app", "postgresql://db"],
)
def test_store_update_rejects_invalid_database_urls(database_url):
    from app.stores.models import CredentialSet, StoreUpdate

    with pytest.raises(ValidationError, match="db_url"):
        StoreUpdate(db_url=database_url)
    with pytest.raises(ValidationError):
        CredentialSet(key="DATABASE_URL", value=database_url)


@pytest.mark.asyncio
async def test_disconnect_store_pool_evicts_and_disconnects_previous_pool():
    from app.stores import api

    pool = MagicMock()
    pool.disconnect = AsyncMock()
    old_url = "postgresql://old:secret@db/old"
    api._store_pools[old_url] = (pool, 0.0)
    try:
        await api._disconnect_store_pool(old_url)
        assert old_url not in api._store_pools
        pool.disconnect.assert_awaited_once()
    finally:
        api._store_pools.clear()


@pytest.mark.asyncio
async def test_store_detail_masks_database_url_and_never_returns_decrypted_value(monkeypatch):
    from app.stores import api

    row = MagicMock()
    row._mapping = {"id": "store-1", "db_url_encrypted": "ciphertext", "name": "Store"}
    monkeypatch.setattr(api.db, "fetch_one", AsyncMock(return_value=row))
    monkeypatch.setattr(api, "decrypt", lambda value: "postgresql://user:secret@db/store")
    monkeypatch.setattr(api, "mask_database_url", lambda value: "postgresql://***:***@db/store")

    result = await api.get_store("store-1")

    assert result["db_url_masked"] == "postgresql://***:***@db/store"
    assert "db_url_encrypted" not in result
    assert "secret" not in str(result)
