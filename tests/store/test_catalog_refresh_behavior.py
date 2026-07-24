import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def catalog_state():
    from app.catalog import sheets

    names = (
        "_catalog_cache",
        "_catalog_ts",
        "_refresh_failures",
        "_next_refresh_allowed",
        "_refresh_generation",
        "_last_refresh_succeeded",
    )
    original = {name: getattr(sheets, name) for name in names}
    sheets._catalog_cache = []
    sheets._catalog_ts = 0
    sheets._refresh_failures = 0
    sheets._next_refresh_allowed = 0
    sheets._refresh_generation = 0
    sheets._last_refresh_succeeded = False
    try:
        yield sheets
    finally:
        for name, value in original.items():
            setattr(sheets, name, value)


def test_cached_catalog_read_never_triggers_google_sheets_io(monkeypatch, catalog_state):
    refresh = MagicMock()
    monkeypatch.setattr(catalog_state, "refresh_catalog", refresh)

    assert catalog_state.get_cached_catalog() == []
    refresh.assert_not_called()


@pytest.mark.asyncio
async def test_async_refresh_is_offloaded_to_worker_thread(monkeypatch, catalog_state):
    to_thread = AsyncMock(return_value=True)
    monkeypatch.setattr(catalog_state.asyncio, "to_thread", to_thread)

    result = await catalog_state.refresh_catalog_async(force=True)

    assert result is True
    to_thread.assert_awaited_once_with(catalog_state.refresh_catalog, force=True)


def test_failed_refresh_uses_backoff_and_preserves_cache(monkeypatch, catalog_state):
    catalog_state._catalog_cache = [{"sku": "existing"}]
    get_client = MagicMock(side_effect=RuntimeError("Sheets unavailable"))
    monkeypatch.setattr(catalog_state, "_get_gspread_client", get_client)
    monkeypatch.setattr(catalog_state.time, "monotonic", lambda: 100.0)

    assert catalog_state.refresh_catalog() is False
    assert catalog_state.refresh_catalog() is False

    assert get_client.call_count == 1
    assert catalog_state._catalog_cache == [{"sku": "existing"}]
    assert catalog_state._next_refresh_allowed == 160.0


def test_forced_refresh_bypasses_failure_backoff(monkeypatch, catalog_state):
    get_client = MagicMock(side_effect=RuntimeError("Sheets unavailable"))
    monkeypatch.setattr(catalog_state, "_get_gspread_client", get_client)
    monkeypatch.setattr(catalog_state.time, "monotonic", lambda: 100.0)

    assert catalog_state.refresh_catalog() is False
    assert catalog_state.refresh_catalog(force=True) is False
    assert get_client.call_count == 2


@pytest.mark.asyncio
async def test_concurrent_async_refreshes_share_one_sheets_request(monkeypatch, catalog_state):
    records = [{
        "SKU": "SKU-1",
        "Product name": "Pijama",
        "Category": "Pijamas",
        "Size": "M",
        "Price USD": 28,
        "Stock": 2,
        "Active": "yes",
    }]
    worksheet = MagicMock()

    def get_records(**kwargs):
        time.sleep(0.03)
        return records

    worksheet.get_all_records.side_effect = get_records
    client = SimpleNamespace(open_by_key=lambda key: SimpleNamespace(sheet1=worksheet))
    get_client = MagicMock(return_value=client)
    monkeypatch.setattr(catalog_state, "_get_gspread_client", get_client)
    monkeypatch.setattr(catalog_state, "get_config", lambda: SimpleNamespace(product_sheet_id="sheet-1"))

    results = await asyncio.gather(
        catalog_state.refresh_catalog_async(),
        catalog_state.refresh_catalog_async(),
    )

    assert results == [True, True]
    assert get_client.call_count == 1
    assert catalog_state.get_cached_catalog()[0]["sku"] == "SKU-1"


@pytest.mark.asyncio
async def test_ensure_fresh_catalog_refreshes_stale_cache(monkeypatch, catalog_state):
    catalog_state._catalog_cache = [{"sku": "OLD"}]
    catalog_state._catalog_ts = time.time() - catalog_state.catalog_max_age_seconds() - 1
    refreshed = [{"sku": "NEW"}]

    async def refresh(force=False):
        assert force is False
        catalog_state._catalog_cache = refreshed
        catalog_state._catalog_ts = time.time()
        return True

    monkeypatch.setattr(catalog_state, "refresh_catalog_async", refresh)

    assert await catalog_state.ensure_fresh_catalog() == refreshed


@pytest.mark.asyncio
async def test_ensure_fresh_catalog_fails_closed_when_refresh_cannot_prove_freshness(monkeypatch, catalog_state):
    catalog_state._catalog_cache = [{"sku": "OLD"}]
    catalog_state._catalog_ts = time.time() - catalog_state.catalog_max_age_seconds() - 1
    monkeypatch.setattr(catalog_state, "refresh_catalog_async", AsyncMock(return_value=False))

    with pytest.raises(catalog_state.InventoryUpdateError, match="stale"):
        await catalog_state.ensure_fresh_catalog()
