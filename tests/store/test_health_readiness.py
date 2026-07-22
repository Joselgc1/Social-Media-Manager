import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _config():
    return SimpleNamespace(
        channel_backend="meta",
        whatsapp_access_token="wa-token",
        instagram_access_token="",
    )


def _catalog():
    return [{
        "sku": "SKU-M",
        "parent_sku": "SKU",
        "product_name": "Pijama",
        "category": "Pijamas",
        "size": "M",
        "sizes": "M",
        "price_usd": 28,
        "stock": 2,
    }]


def _payload(response):
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_health_is_ready_only_with_catalog_scheduler_database_and_provider(monkeypatch):
    from app import main

    monkeypatch.setattr(main, "get_config", _config)
    monkeypatch.setattr(main, "get_cached_catalog", _catalog)
    monkeypatch.setattr(main, "get_scheduler", lambda: SimpleNamespace(running=True))
    monkeypatch.setattr(main, "list_providers", lambda: ["openai"])
    monkeypatch.setattr(
        main.db,
        "get_settings",
        AsyncMock(return_value={"llm_provider": "openai", "llm_model": "gpt", "auto_fallback": True}),
    )
    monkeypatch.setattr(main.db, "fetch_one", AsyncMock(return_value={"cnt": 2}))

    response = await main.health()

    assert response.status_code == 200
    payload = _payload(response)
    assert payload["status"] == "healthy"
    assert payload["catalog_products"] == 1
    assert payload["scheduler"] == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize(("catalog", "scheduler_running"), [([], True), (_catalog(), False)])
async def test_health_returns_503_for_empty_catalog_or_stopped_scheduler(
    monkeypatch, catalog, scheduler_running
):
    from app import main

    monkeypatch.setattr(main, "get_config", _config)
    monkeypatch.setattr(main, "get_cached_catalog", lambda: catalog)
    monkeypatch.setattr(main, "get_scheduler", lambda: SimpleNamespace(running=scheduler_running))
    monkeypatch.setattr(main, "list_providers", lambda: ["openai"])
    monkeypatch.setattr(main.db, "get_settings", AsyncMock(return_value={"llm_provider": "openai"}))
    monkeypatch.setattr(main.db, "fetch_one", AsyncMock(return_value={"cnt": 0}))

    response = await main.health()

    assert response.status_code == 503
    assert _payload(response)["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_health_catalog_read_does_not_trigger_external_refresh(monkeypatch):
    from app import main
    from app.catalog import sheets

    refresh = AsyncMock()
    monkeypatch.setattr(sheets, "refresh_catalog_async", refresh)
    monkeypatch.setattr(main, "get_config", _config)
    monkeypatch.setattr(main, "get_cached_catalog", lambda: [])
    monkeypatch.setattr(main, "get_scheduler", lambda: SimpleNamespace(running=True))
    monkeypatch.setattr(main, "list_providers", lambda: ["openai"])
    monkeypatch.setattr(main.db, "get_settings", AsyncMock(return_value={"llm_provider": "openai"}))
    monkeypatch.setattr(main.db, "fetch_one", AsyncMock(return_value={"cnt": 0}))

    await main.health()

    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_health_returns_503_when_database_probe_fails(monkeypatch):
    from app import main

    monkeypatch.setattr(main, "get_config", _config)
    monkeypatch.setattr(main, "get_cached_catalog", _catalog)
    monkeypatch.setattr(main, "get_scheduler", lambda: SimpleNamespace(running=True))
    monkeypatch.setattr(main, "list_providers", lambda: ["openai"])
    monkeypatch.setattr(main.db, "get_settings", AsyncMock(side_effect=RuntimeError("db down")))

    response = await main.health()

    assert response.status_code == 503
    assert _payload(response)["database"] == "error"
