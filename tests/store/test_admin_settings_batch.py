import json
from pathlib import Path
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


@pytest.mark.asyncio
async def test_settings_batch_validates_provider_before_model_and_writes_once(monkeypatch):
    from app.admin import settings

    execute = AsyncMock()
    invalidate = MagicMock()
    monkeypatch.setattr(settings, "get_config", lambda: SimpleNamespace(llm_managed_externally=False))
    monkeypatch.setattr(
        settings.db,
        "get_settings",
        AsyncMock(return_value={"llm_provider": "openai", "llm_model": "gpt-5.6-luna"}),
    )
    monkeypatch.setattr(settings.db, "get_db", lambda: _database())
    monkeypatch.setattr(settings.db, "execute", execute)
    monkeypatch.setattr(settings.db, "invalidate_settings_cache", invalidate)

    result = await settings._apply_settings_batch({
        "llm_model": "claude-haiku-4-5",
        "llm_provider": "anthropic",
        "llm_temperature": 0.3,
    })

    assert result == {
        "llm_provider": "anthropic",
        "llm_model": "claude-haiku-4-5",
        "llm_temperature": 0.3,
    }
    execute.assert_awaited_once()
    query, values = execute.await_args.args
    assert "jsonb_each" in query
    assert json.loads(values["updates"]) == result
    invalidate.assert_called_once()


@pytest.mark.asyncio
async def test_invalid_batch_does_not_write_any_setting(monkeypatch):
    from app.admin import settings

    execute = AsyncMock()
    monkeypatch.setattr(settings, "get_config", lambda: SimpleNamespace(llm_managed_externally=False))
    monkeypatch.setattr(settings.db, "get_settings", AsyncMock(return_value={"llm_provider": "openai"}))
    monkeypatch.setattr(settings.db, "execute", execute)

    with pytest.raises(HTTPException, match="not available for provider"):
        await settings._apply_settings_batch({
            "order_discount_percent": 10,
            "llm_model": "claude-haiku-4-5",
        })

    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_externally_managed_batch_rejects_all_changes_before_write(monkeypatch):
    from app.admin import settings

    execute = AsyncMock()
    get_settings = AsyncMock()
    monkeypatch.setattr(settings, "get_config", lambda: SimpleNamespace(llm_managed_externally=True))
    monkeypatch.setattr(settings.db, "get_settings", get_settings)
    monkeypatch.setattr(settings.db, "execute", execute)

    with pytest.raises(HTTPException, match="managed by the master"):
        await settings._apply_settings_batch({
            "store_phone_number": "+58 412 1234567",
            "llm_temperature": 0.4,
        })

    get_settings.assert_not_awaited()
    execute.assert_not_awaited()


def test_dashboard_rejects_http_errors_and_uses_batch_for_grouped_saves():
    dashboard = Path("store/app/static/js/dashboard.js").read_text()

    assert "if (!resp.ok)" in dashboard
    assert "error.status = resp.status" in dashboard
    assert "throw error" in dashboard
    assert "API + '/batch'" in dashboard
    assert "Promise.all([\n    apiFetch(API + '/order_discount_percent'" not in dashboard
    assert "'/switch-provider?provider='" not in dashboard
