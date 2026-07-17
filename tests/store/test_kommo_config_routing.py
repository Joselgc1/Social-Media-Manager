import importlib
import sys
from types import SimpleNamespace

import pytest
from app.config import Settings, get_config
from pydantic import ValidationError


def _base_config(**overrides):
    data = {
        "channel_backend": "meta",
        "debug": False,
        "admin_password": "admin",
        "openai_api_key": "sk-test",
        "anthropic_api_key": "",
        "meta_app_secret": "meta-secret",
        "whatsapp_access_token": "wa-token",
        "whatsapp_phone_number_id": "phone-id",
        "whatsapp_verify_token": "verify",
        "instagram_access_token": "",
        "instagram_verify_token": "",
        "telegram_bot_token": "",
        "telegram_admin_chat_id": "",
        "kommo_subdomain": "store",
        "kommo_access_token": "kommo-token",
        "kommo_integration_id": "client-uuid",
        "kommo_integration_secret": "kommo-secret",
        "kommo_salesbot_id": 123,
        "kommo_webhook_secret": "webhook-secret",
        "kommo_ai_mode_field_id": 111,
        "kommo_ai_active_enum_id": 222,
        "kommo_ai_human_enum_id": 333,
        "kommo_ai_paused_enum_id": 444,
        "kommo_default_responsible_user_id": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_meta_mode_startup_validation_requires_meta_credentials():
    from app.main import _validate_startup_config

    with pytest.raises(RuntimeError, match="WHATSAPP_ACCESS_TOKEN"):
        _validate_startup_config(_base_config(whatsapp_access_token=""))


def test_kommo_mode_startup_validation_does_not_require_meta_credentials():
    from app.main import _validate_startup_config

    _validate_startup_config(
        _base_config(
            channel_backend="kommo",
            meta_app_secret="",
            whatsapp_access_token="",
            whatsapp_phone_number_id="",
            whatsapp_verify_token="",
        )
    )


def test_kommo_mode_startup_validation_requires_kommo_credentials():
    from app.main import _validate_startup_config

    with pytest.raises(RuntimeError, match="KOMMO_INTEGRATION_ID"):
        _validate_startup_config(_base_config(channel_backend="kommo", kommo_integration_id=""))


def test_kommo_mode_startup_validation_rejects_invalid_subdomain():
    from app.main import _validate_startup_config

    with pytest.raises(RuntimeError, match="KOMMO_SUBDOMAIN"):
        _validate_startup_config(_base_config(channel_backend="kommo", kommo_subdomain="acme.kommo.com/evil"))


def test_invalid_channel_backend_rejected(monkeypatch):
    monkeypatch.setenv("CHANNEL_BACKEND", "bad")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("GOOGLE_SHEETS_CREDENTIALS_B64", "e30=")
    monkeypatch.setenv("PRODUCT_SHEET_ID", "sheet")
    with pytest.raises(ValidationError):
        Settings()


def test_optional_kommo_responsible_user_accepts_empty_string(monkeypatch):
    monkeypatch.setenv("KOMMO_DEFAULT_RESPONSIBLE_USER_ID", "")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("GOOGLE_SHEETS_CREDENTIALS_B64", "e30=")
    monkeypatch.setenv("PRODUCT_SHEET_ID", "sheet")
    assert Settings().kommo_default_responsible_user_id is None


def _load_main_for_backend(monkeypatch, backend: str):
    monkeypatch.setenv("CHANNEL_BACKEND", backend)
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("GOOGLE_SHEETS_CREDENTIALS_B64", "e30=")
    monkeypatch.setenv("PRODUCT_SHEET_ID", "sheet")
    get_config.cache_clear()
    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main")


def _route_paths(app):
    return {route.path for route in app.routes}


def test_meta_routes_registered_only_in_meta_mode(monkeypatch):
    main = _load_main_for_backend(monkeypatch, "meta")
    paths = _route_paths(main.app)
    assert "/webhooks/whatsapp" in paths
    assert "/webhooks/instagram" in paths
    assert "/webhooks/kommo/salesbot" not in paths
    assert "/admin/settings/" in paths
    assert "/test/chat" in paths


def test_kommo_routes_registered_only_in_kommo_mode(monkeypatch):
    main = _load_main_for_backend(monkeypatch, "kommo")
    paths = _route_paths(main.app)
    assert "/webhooks/kommo/events/{webhook_secret}" in paths
    assert "/webhooks/kommo/salesbot" in paths
    assert "/webhooks/whatsapp" not in paths
    assert "/webhooks/instagram" not in paths
    assert "/admin/settings/" in paths
    assert "/test/chat" in paths
