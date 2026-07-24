import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def telegram_config():
    return SimpleNamespace(
        telegram_admin_chat_id="123456",
        telegram_webhook_secret="valid_webhook-secret",
    )


def test_telegram_bot_token_is_redacted_from_http_logs(caplog):
    from app.log_redaction import install_secret_redaction_filter

    install_secret_redaction_filter()
    caplog.set_level(logging.INFO, logger="httpx")

    logging.getLogger("httpx").info(
        'HTTP Request: POST https://api.telegram.org/bot123456:ABC-SECRET/setWebhook "HTTP/1.1 200 OK"'
    )

    assert "123456:ABC-SECRET" not in caplog.text
    assert "https://api.telegram.org/bot<redacted>/setWebhook" in caplog.text


@pytest.mark.parametrize("headers", [{}, {"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"}])
@pytest.mark.asyncio
async def test_forged_telegram_update_is_rejected_before_command_dispatch(headers, telegram_config):
    from app.admin import telegram_bot

    app = FastAPI()
    app.include_router(telegram_bot.router)
    handle_command = AsyncMock(return_value="changed")

    with (
        patch.object(telegram_bot, "get_config", return_value=telegram_config),
        patch.object(telegram_bot, "_handle_command", handle_command),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/webhooks/telegram",
                headers=headers,
                json={"message": {"chat": {"id": 123456}, "text": "/ai off"}},
            )

    assert response.status_code == 403
    handle_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_authenticated_telegram_update_preserves_command_flow(telegram_config):
    from app.admin import telegram_bot

    app = FastAPI()
    app.include_router(telegram_bot.router)
    handle_command = AsyncMock(return_value="AI paused")
    notify_owner = AsyncMock()

    with (
        patch.object(telegram_bot, "get_config", return_value=telegram_config),
        patch.object(telegram_bot, "_handle_command", handle_command),
        patch.object(telegram_bot, "notify_owner", notify_owner),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/webhooks/telegram",
                headers={"X-Telegram-Bot-Api-Secret-Token": "valid_webhook-secret"},
                json={"message": {"chat": {"id": 123456}, "text": "/ai off"}},
            )

    assert response.status_code == 200
    handle_command.assert_awaited_once_with("/ai", "off")
    notify_owner.assert_awaited_once_with("AI paused")


@pytest.mark.asyncio
async def test_webhook_registration_sends_secret_token():
    from app.admin import telegram_bot

    response = MagicMock()
    response.json.return_value = {"ok": True}
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    context_manager = MagicMock()
    context_manager.__aenter__ = AsyncMock(return_value=client)
    context_manager.__aexit__ = AsyncMock(return_value=None)

    with patch.object(telegram_bot.httpx, "AsyncClient", return_value=context_manager):
        result = await telegram_bot.setup_telegram_webhook(
            "bot-token",
            "https://store.example.com/webhooks/telegram",
            "valid_webhook-secret",
        )

    assert result == {"ok": True}
    client.post.assert_awaited_once_with(
        "https://api.telegram.org/botbot-token/setWebhook",
        json={
            "url": "https://store.example.com/webhooks/telegram",
            "secret_token": "valid_webhook-secret",
        },
    )
