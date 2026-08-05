from unittest.mock import AsyncMock

import pytest
from app import db


def test_runtime_fallback_validation_accepts_available_provider():
    db.validate_runtime_provider_settings(
        {"auto_fallback": True, "fallback_provider": "anthropic"},
        available_providers=["openai", "anthropic"],
    )


def test_runtime_fallback_validation_rejects_unavailable_provider():
    with pytest.raises(RuntimeError, match="fallback provider 'anthropic' is not initialized"):
        db.validate_runtime_provider_settings(
            {"auto_fallback": True, "fallback_provider": "anthropic"},
            available_providers=["openai"],
        )


def test_runtime_fallback_validation_allows_disabled_fallback():
    db.validate_runtime_provider_settings(
        {"auto_fallback": False, "fallback_provider": "anthropic"},
        available_providers=["openai"],
    )


def test_runtime_fallback_validation_skips_before_provider_initialization():
    db.validate_runtime_provider_settings(
        {"auto_fallback": True, "fallback_provider": "anthropic"},
        available_providers=[],
    )


@pytest.mark.asyncio
async def test_get_settings_rejects_invalid_stored_fallback_after_provider_init(monkeypatch):
    from app.ai import providers

    db.invalidate_settings_cache()
    monkeypatch.setattr(db, "fetch_one", AsyncMock(return_value={"version": "v1"}))
    monkeypatch.setattr(
        db,
        "fetch_all",
        AsyncMock(return_value=[
            {"key": "llm_provider", "value": '"openai"'},
            {"key": "auto_fallback", "value": True},
            {"key": "fallback_provider", "value": '"anthropic"'},
        ]),
    )
    monkeypatch.setattr(providers, "list_providers", lambda: ["openai"])

    try:
        with pytest.raises(RuntimeError, match="fallback provider 'anthropic' is not initialized"):
            await db.get_settings()
    finally:
        db.invalidate_settings_cache()


@pytest.mark.asyncio
async def test_get_settings_accepts_valid_stored_fallback_after_provider_init(monkeypatch):
    from app.ai import providers

    db.invalidate_settings_cache()
    monkeypatch.setattr(db, "fetch_one", AsyncMock(return_value={"version": "v1"}))
    monkeypatch.setattr(
        db,
        "fetch_all",
        AsyncMock(return_value=[
            {"key": "llm_provider", "value": '"openai"'},
            {"key": "auto_fallback", "value": True},
            {"key": "fallback_provider", "value": '"anthropic"'},
        ]),
    )
    monkeypatch.setattr(providers, "list_providers", lambda: ["openai", "anthropic"])

    try:
        settings = await db.get_settings()
    finally:
        db.invalidate_settings_cache()

    assert settings["fallback_provider"] == "anthropic"
