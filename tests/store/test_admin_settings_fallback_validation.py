import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException


def _database():
    database = MagicMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    database.transaction.return_value = transaction
    return database


def _patch_db(monkeypatch, current_settings, execute=None):
    from app.admin import settings

    execute = execute or AsyncMock()
    monkeypatch.setattr(settings, "get_config", lambda: SimpleNamespace(llm_managed_externally=False))
    monkeypatch.setattr(settings.db, "get_settings", AsyncMock(return_value=dict(current_settings)))
    monkeypatch.setattr(settings.db, "get_db", lambda: _database())
    monkeypatch.setattr(settings.db, "execute", execute)
    monkeypatch.setattr(settings.db, "invalidate_settings_cache", MagicMock())
    return execute


@pytest.mark.asyncio
async def test_enabling_auto_fallback_with_unavailable_provider_is_rejected(monkeypatch):
    from app.admin import settings

    execute = _patch_db(
        monkeypatch,
        {
            "auto_fallback": False,
            "fallback_provider": "anthropic",
            "fallback_model": "claude-haiku-4-5",
        },
    )
    monkeypatch.setattr(settings, "list_providers", lambda: ["openai"])

    with pytest.raises(HTTPException, match="is not available"):
        await settings._apply_settings_batch({"auto_fallback": True})

    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_setting_unavailable_fallback_provider_is_rejected(monkeypatch):
    from app.admin import settings

    execute = _patch_db(
        monkeypatch,
        {
            "auto_fallback": True,
            "fallback_provider": "openai",
            "fallback_model": "gpt-5.6-luna",
        },
    )
    monkeypatch.setattr(settings, "list_providers", lambda: ["openai"])

    with pytest.raises(HTTPException, match="is not available"):
        await settings._apply_settings_batch({"fallback_provider": "anthropic"})

    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_fallback_with_available_provider_is_written(monkeypatch):
    from app.admin import settings

    execute = _patch_db(
        monkeypatch,
        {
            "auto_fallback": False,
            "fallback_provider": "anthropic",
            "fallback_model": "claude-haiku-4-5",
        },
    )
    monkeypatch.setattr(settings, "list_providers", lambda: ["openai"])

    result = await settings._apply_settings_batch(
        {
            "auto_fallback": True,
            "fallback_provider": "openai",
            "fallback_model": "gpt-5.6-luna",
        }
    )

    assert result == {
        "auto_fallback": True,
        "fallback_provider": "openai",
        "fallback_model": "gpt-5.6-luna",
    }
    execute.assert_awaited_once()
    query, values = execute.await_args.args
    assert json.loads(values["updates"]) == result


@pytest.mark.asyncio
async def test_unrelated_batch_does_not_reject_preexisting_fallback_config(monkeypatch):
    from app.admin import settings

    execute = _patch_db(
        monkeypatch,
        {
            "auto_fallback": True,
            "fallback_provider": "anthropic",
            "fallback_model": "claude-haiku-4-5",
        },
    )
    monkeypatch.setattr(settings, "list_providers", lambda: ["openai"])

    result = await settings._apply_settings_batch({"llm_temperature": 0.4})

    assert result == {"llm_temperature": 0.4}
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_runtime_providers_skips_availability_check(monkeypatch):
    from app.admin import settings

    execute = _patch_db(
        monkeypatch,
        {
            "auto_fallback": False,
            "fallback_provider": "anthropic",
            "fallback_model": "claude-haiku-4-5",
        },
    )
    monkeypatch.setattr(settings, "list_providers", lambda: [])

    result = await settings._apply_settings_batch({"auto_fallback": True})

    assert result == {"auto_fallback": True}
    execute.assert_awaited_once()
