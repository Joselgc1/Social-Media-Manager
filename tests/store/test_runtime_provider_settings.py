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
