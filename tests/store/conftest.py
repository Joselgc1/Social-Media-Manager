"""
Shared test fixtures for store app tests.

Provides mock database, mock config, and FastAPI test client
so tests run without real external services.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("GOOGLE_SHEETS_CREDENTIALS_B64", "e30=")  # base64 "{}"
os.environ.setdefault("PRODUCT_SHEET_ID", "test-sheet-id")


class MockRecord(dict):
    """Simulates a databases library record that supports bracket access."""
    def __getitem__(self, key):
        return super().__getitem__(key)


def make_record(**kwargs) -> MockRecord:
    """Create a mock database record from keyword arguments."""
    return MockRecord(kwargs)


@pytest.fixture
def sanitized_a105_native_instagram_comment_payload():
    """Sanitized Kommo A105 mirrored Instagram comment general-webhook payload."""
    return {
        "account[id]": "1000001",
        "account[subdomain]": "acme",
        "message[add][0][id]": "A105",
        "message[add][0][chat_id]": "chat-a105",
        "message[add][0][talk_id]": "105",
        "message[add][0][contact_id]": "420105",
        "message[add][0][entity_id]": "105105",
        "message[add][0][entity_type]": "lead",
        "message[add][0][text]": "Precio?",
        "message[add][0][message_type]": "text",
        "message[add][0][origin]": "instagram_business",
        "message[add][0][type]": "incoming",
        "message[add][0][author][id]": "ig-author-a105",
        "message[add][0][author][name]": "Cliente Instagram",
        "message[add][0][author][type]": "external",
    }


@pytest.fixture
def mock_db():
    """Mock database with common async methods."""
    db = MagicMock()
    db.fetch_one = AsyncMock(return_value=None)
    db.fetch_all = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=None)
    db.get_settings = AsyncMock(return_value={
        "ai_provider": "openai",
        "ai_model": "gpt-5.6-luna",
        "ai_temperature": "0.7",
        "ai_max_tokens": "500",
        "ai_enabled": "true",
        "max_conversation_history": "20",
        "fallback_provider": "",
        "fallback_model": "",
        "fallback_auto_enable": "false",
        "catalog_pdf_interval_hours": "24",
    })
    db.invalidate_settings_cache = MagicMock()
    return db


@pytest.fixture
def mock_config():
    """Mock Settings object with test values."""
    config = MagicMock()
    config.database_url = "postgresql://test:test@localhost:5432/test"
    config.openai_api_key = "sk-test-key"
    config.anthropic_api_key = ""
    config.google_sheets_credentials_b64 = "e30="
    config.product_sheet_id = "test-sheet-id"
    config.telegram_bot_token = "test-bot-token"
    config.telegram_admin_chat_id = "12345"
    config.telegram_webhook_secret = "test-telegram-webhook-secret"
    config.store_name = "Test Store"
    config.owner_name = "Test Owner"
    config.app_base_url = "http://localhost:8000"
    config.debug = True
    config.admin_password = "test-password"
    config.system_prompt_override = ""
    config.llm_managed_externally = False
    config.ai_orchestration_mode = "legacy"
    config.meta_app_secret = ""
    config.whatsapp_access_token = ""
    config.whatsapp_phone_number_id = ""
    config.whatsapp_verify_token = "test-verify"
    config.instagram_access_token = ""
    config.instagram_verify_token = "test-verify"
    return config


@pytest.fixture
async def client(mock_db, mock_config):
    """FastAPI test client with mocked dependencies."""
    from httpx import ASGITransport, AsyncClient

    with patch("app.db.database", mock_db), \
         patch("app.config.get_config", return_value=mock_config):
        from app.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
