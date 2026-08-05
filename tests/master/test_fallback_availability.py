from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


def _record(**kwargs):
    class Rec(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    return Rec(kwargs)


@pytest.mark.asyncio
async def test_auto_fallback_enabled_without_api_key_is_rejected(monkeypatch):
    from app.stores import api

    api.db.fetch_all = AsyncMock(return_value=[])
    with pytest.raises(HTTPException, match="no configured API key"):
        await api._enforce_fallback_provider_availability(
            "store-1",
            {"auto_fallback": True},
            {"fallback_provider": "anthropic"},
        )
    api.db.fetch_all.assert_called_once()


@pytest.mark.asyncio
async def test_auto_fallback_with_api_key_passes(monkeypatch):
    from app.stores import api

    api.db.fetch_all = AsyncMock(return_value=[_record(key="ANTHROPIC_API_KEY", value_encrypted="enc")])
    monkeypatch.setattr(api, "decrypt", lambda _v: "sk-ant-abc123")
    await api._enforce_fallback_provider_availability(
        "store-1",
        {"auto_fallback": True},
        {"fallback_provider": "anthropic"},
    )


@pytest.mark.asyncio
async def test_auto_fallback_with_placeholder_api_key_is_rejected(monkeypatch):
    from app.stores import api

    api.db.fetch_all = AsyncMock(return_value=[_record(key="ANTHROPIC_API_KEY", value_encrypted="enc")])
    monkeypatch.setattr(api, "decrypt", lambda _v: "change-me")
    with pytest.raises(HTTPException, match="no configured API key"):
        await api._enforce_fallback_provider_availability(
            "store-1",
            {"auto_fallback": True},
            {"fallback_provider": "anthropic"},
        )


@pytest.mark.asyncio
async def test_batch_without_fallback_keys_skips_check(monkeypatch):
    from app.stores import api

    api.db.fetch_all = AsyncMock(return_value=[])
    await api._enforce_fallback_provider_availability(
        "store-1",
        {"llm_temperature": 0.4},
        {"auto_fallback": True, "fallback_provider": "anthropic"},
    )
    api.db.fetch_all.assert_not_called()


@pytest.mark.asyncio
async def test_auto_fallback_disabled_skips_check(monkeypatch):
    from app.stores import api

    api.db.fetch_all = AsyncMock(return_value=[])
    await api._enforce_fallback_provider_availability(
        "store-1",
        {"auto_fallback": False, "fallback_provider": "anthropic"},
        {},
    )
    api.db.fetch_all.assert_not_called()
