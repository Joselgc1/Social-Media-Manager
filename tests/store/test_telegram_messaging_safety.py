"""
Tests for Telegram outbound messaging safety and admin command hardening.

Covers: markdown escaping, 4096-char chunking, Telegram API status checks,
graceful failure handling, /order authoritative statuses, /settings
conciseness, and /customers recency sorting.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.crm import orders


@pytest.fixture
def sender_config():
    return SimpleNamespace(
        telegram_bot_token="123456:TEST-TOKEN",
        telegram_admin_chat_id="12345",
    )


def _telegram_client_context(responses):
    """Build a mocked httpx.AsyncClient context that yields the given responses."""
    client = MagicMock()
    client.post = AsyncMock(side_effect=responses)
    context_manager = MagicMock()
    context_manager.__aenter__ = AsyncMock(return_value=client)
    context_manager.__aexit__ = AsyncMock(return_value=None)
    return client, context_manager


class _FakeTelegramResponse:
    def __init__(self, ok, status_code=200):
        self.status_code = status_code
        self._ok = ok

    def json(self):
        return {"ok": self._ok}


# -- Markdown escaping --------------------------------------------------


def test_escape_markdown_escapes_telegram_special_characters():
    from app.admin.telegram_sender import escape_markdown

    result = escape_markdown("100% *genial* [enlace](url) `codigo` _italica_ \\path")

    assert "\\*genial\\*" in result
    assert "\\[enlace\\]" in result
    assert "\\`codigo\\`" in result
    assert "\\_italica\\_" in result
    assert "\\\\path" in result
    assert "*genial*" not in result
    assert "[enlace]" not in result
    assert "\\](url)" in result


def test_escape_markdown_handles_none_and_empty():
    from app.admin.telegram_sender import escape_markdown

    assert escape_markdown(None) == ""
    assert escape_markdown("") == ""


# -- Message chunking ----------------------------------------------------


def test_split_message_chunks_long_text_without_exceeding_limit():
    from app.admin.telegram_sender import MAX_MESSAGE_LENGTH, split_message

    text = "x" * (MAX_MESSAGE_LENGTH * 2 + 500)
    chunks = split_message(text)

    assert len(chunks) == 3
    assert all(len(chunk) <= MAX_MESSAGE_LENGTH for chunk in chunks)
    assert "".join(chunks) == text


def test_split_message_prefers_newline_boundaries():
    from app.admin.telegram_sender import split_message

    text = ("a" * 4000) + "\n" + ("b" * 4000)
    chunks = split_message(text)

    assert chunks[0].endswith("a")
    assert chunks[1].startswith("b")
    assert all(len(chunk) <= 4096 for chunk in chunks)


def test_split_message_returns_empty_for_blank_text():
    from app.admin.telegram_sender import split_message

    assert split_message("") == []
    assert split_message("   \n ") == []


def _assert_safe_chunks(chunks, text):
    """Every chunk is non-empty, <=4096 chars, content-preserving,
    and never ends directly after a dangling Markdown escape character."""
    assert chunks
    assert all(chunk for chunk in chunks)
    assert all(len(chunk) <= 4096 for chunk in chunks)
    assert "".join(chunks) == text
    assert all(not chunk.endswith("\\") for chunk in chunks)


def test_split_message_never_cuts_after_dangling_escape():
    from app.admin.telegram_sender import split_message

    # The escape character lands exactly on the 4096 boundary; the safe cut
    # must move before it so no chunk ends with a dangling backslash.
    text = ("a" * 4095) + "\\*" + ("b" * 100)
    chunks = split_message(text)

    _assert_safe_chunks(chunks, text)
    assert chunks[0] == "a" * 4095
    assert chunks[1].startswith("\\*")


def test_split_message_avoids_cut_inside_bold_span():
    from app.admin.telegram_sender import split_message

    # Bold opener is inside the window but its closer falls after the limit.
    text = ("a" * 4090) + "*negrita*" + ("b" * 50)
    chunks = split_message(text)

    _assert_safe_chunks(chunks, text)
    assert chunks[0] == "a" * 4090
    assert chunks[0].count("*") == 0
    assert chunks[1].count("*") == 2


def test_split_message_avoids_cut_inside_code_span():
    from app.admin.telegram_sender import split_message

    text = ("a" * 4090) + "`codigo`" + ("b" * 50)
    chunks = split_message(text)

    _assert_safe_chunks(chunks, text)
    assert chunks[0] == "a" * 4090
    assert chunks[1].startswith("`codigo`")


def test_split_message_avoids_cut_inside_link():
    from app.admin.telegram_sender import split_message

    # The closing paren of the link falls after the limit, so the cut must
    # move back to a position with balanced brackets/parens.
    text = ("a" * 4000) + "[t](https://example.com/" + ("x" * 90) + ")"
    chunks = split_message(text)

    _assert_safe_chunks(chunks, text)
    assert chunks[0] == "a" * 4000 + "[t]"
    assert chunks[1].startswith("(https://example.com/")


def test_split_message_ignores_escaped_markers_when_checking_balance():
    from app.admin.telegram_sender import split_message

    # Escaped markers (from escape_markdown) are literal text; cutting inside
    # them is safe, so the cut must NOT retreat before the escape pair.
    text = ("a" * 4090) + "\\*literal\\*" + ("b" * 100)
    chunks = split_message(text)

    _assert_safe_chunks(chunks, text)
    assert chunks[0].startswith("a" * 4090)
    assert "\\*" in chunks[0]
    assert len(chunks[0]) == 4096


def test_split_message_moves_cut_before_unbalanced_newline():
    from app.admin.telegram_sender import split_message

    # A newline sits inside a bold span; the preferred newline cut would
    # split the span, so the cut must move before its opener.
    text = ("a" * 4000) + "*n\n e*" + ("b" * 4000)
    chunks = split_message(text)

    assert chunks[0] == "a" * 4000
    assert chunks[1].startswith("*n")
    _assert_safe_chunks(chunks, text)


def test_split_message_keeps_exact_4096_chunks_for_plain_text():
    from app.admin.telegram_sender import split_message

    text = "x" * 8692
    chunks = split_message(text)

    assert [len(chunk) for chunk in chunks] == [4096, 4096, 500]
    _assert_safe_chunks(chunks, text)


def test_split_message_forced_cut_pathological_span_still_progresses():
    from app.admin.telegram_sender import split_message

    # A single bold span longer than the whole window cannot be balanced
    # without a parser; it must still make progress with valid chunk sizes
    # and no empty chunks (protected against infinite loops).
    text = "*" + ("y" * 5000) + "*"
    chunks = split_message(text)

    _assert_safe_chunks(chunks, text)
    assert chunks[0] == "*" + ("y" * 4095)


# -- Central sender ------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_splits_long_text_into_multiple_requests(sender_config):
    from app.admin import telegram_sender

    responses = [_FakeTelegramResponse(ok=True), _FakeTelegramResponse(ok=True)]
    client, context_manager = _telegram_client_context(responses)

    with (
        patch.object(telegram_sender.httpx, "AsyncClient", return_value=context_manager),
        patch.object(telegram_sender, "get_config", return_value=sender_config),
    ):
        result = await telegram_sender.send_message("y" * 5000)

    assert result is True
    assert client.post.await_count == 2
    first_call = client.post.await_args_list[0].kwargs["json"]
    second_call = client.post.await_args_list[1].kwargs["json"]
    assert len(first_call["text"]) == 4096
    assert len(second_call["text"]) == 5000 - 4096
    assert first_call["chat_id"] == "12345"
    assert first_call["parse_mode"] == "Markdown"


@pytest.mark.asyncio
async def test_send_message_returns_false_when_telegram_rejects_payload(sender_config):
    from app.admin import telegram_sender

    responses = [_FakeTelegramResponse(ok=False, status_code=400)]
    client, context_manager = _telegram_client_context(responses)

    with (
        patch.object(telegram_sender.httpx, "AsyncClient", return_value=context_manager),
        patch.object(telegram_sender, "get_config", return_value=sender_config),
    ):
        result = await telegram_sender.send_message("mensaje corto")

    assert result is False
    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_send_message_degrades_gracefully_on_network_error(sender_config):
    from app.admin import telegram_sender

    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    context_manager = MagicMock()
    context_manager.__aenter__ = AsyncMock(return_value=client)
    context_manager.__aexit__ = AsyncMock(return_value=None)

    with (
        patch.object(telegram_sender.httpx, "AsyncClient", return_value=context_manager),
        patch.object(telegram_sender, "get_config", return_value=sender_config),
    ):
        result = await telegram_sender.send_message("mensaje")

    assert result is False


@pytest.mark.asyncio
async def test_send_message_skips_when_telegram_not_configured():
    from app.admin import telegram_sender

    config = SimpleNamespace(telegram_bot_token="", telegram_admin_chat_id="12345")
    client_mock = MagicMock()

    with (
        patch.object(telegram_sender.httpx, "AsyncClient", return_value=client_mock),
        patch.object(telegram_sender, "get_config", return_value=config),
    ):
        result = await telegram_sender.send_message("mensaje")

    assert result is False
    client_mock.assert_not_called()


@pytest.mark.asyncio
async def test_send_message_skips_blank_text(sender_config):
    from app.admin import telegram_sender

    client_mock = MagicMock()
    with (
        patch.object(telegram_sender.httpx, "AsyncClient", return_value=client_mock),
        patch.object(telegram_sender, "get_config", return_value=sender_config),
    ):
        result = await telegram_sender.send_message("   \n ")

    assert result is False
    client_mock.assert_not_called()


# -- notify.py composition -----------------------------------------------


@pytest.mark.asyncio
async def test_notify_owner_delegates_to_central_sender():
    from app.admin import notify

    with patch.object(notify, "send_message", AsyncMock()) as send_message:
        await notify.notify_owner("hola admin")

    send_message.assert_awaited_once_with("hola admin")


@pytest.mark.asyncio
async def test_notify_escalation_escapes_customer_content():
    from app.admin import notify

    with patch.object(notify, "send_message", AsyncMock()) as send_message:
        await notify.notify_escalation(
            customer_name="Cliente *VIP* [urgente]",
            customer_channel="whatsapp",
            customer_platform_id="584121234567",
            reason="pedido `incompleto` _ahora_",
            urgency="high",
            conversation_summary="Necesita [ayuda] con su *pedido*",
        )

    message = send_message.await_args.args[0]
    assert "\\*VIP\\*" in message
    assert "\\[urgente\\]" in message
    assert "\\`incompleto\\`" in message
    assert "\\_ahora\\_" in message
    assert "\\*pedido\\*" in message
    assert "*VIP*" not in message


@pytest.mark.asyncio
async def test_notify_incoming_message_escapes_customer_text():
    from app.admin import notify

    with patch.object(notify, "send_message", AsyncMock()) as send_message:
        await notify.notify_incoming_message(
            customer_name="Cliente _Nuevo_",
            customer_channel="instagram",
            customer_platform_id="handle_123",
            message_text="quiero [esto] *ahora*",
        )

    message = send_message.await_args.args[0]
    assert "\\_Nuevo\\_" in message
    assert "\\*ahora\\*" in message
    assert "[esto]" not in message
    assert "\\[esto\\]" in message


# -- /order handler ------------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_update_order_accepts_rejected_payment_status():
    from app.admin import telegram_bot

    update_payment = AsyncMock()
    row = {"id": "abc12345-1111-2222-3333-444444444444"}

    with (
        patch.object(telegram_bot.db, "fetch_one", AsyncMock(return_value=row)),
        patch.object(telegram_bot.orders, "update_order_payment_status", update_payment),
    ):
        result = await telegram_bot._cmd_update_order("abc1 rejected")

    assert "actualizado a *rejected*" in result
    update_payment.assert_awaited_once_with("abc12345-1111-2222-3333-444444444444", status="rejected")


@pytest.mark.asyncio
async def test_cmd_update_order_uses_order_service_for_shipping():
    from app.admin import telegram_bot

    update_shipping = AsyncMock()
    row = {"id": "abc12345-1111-2222-3333-444444444444"}

    with (
        patch.object(telegram_bot.db, "fetch_one", AsyncMock(return_value=row)),
        patch.object(telegram_bot.orders, "update_order_shipping", update_shipping),
    ):
        result = await telegram_bot._cmd_update_order("abc1 shipped")

    assert "actualizado a *shipped*" in result
    update_shipping.assert_awaited_once_with(
        "abc12345-1111-2222-3333-444444444444", shipping_status="shipped"
    )


@pytest.mark.asyncio
async def test_cmd_update_order_rejects_unknown_status():
    from app.admin import telegram_bot

    update_payment = AsyncMock()
    fetch_one = AsyncMock()

    with (
        patch.object(telegram_bot.db, "fetch_one", fetch_one),
        patch.object(telegram_bot.orders, "update_order_payment_status", update_payment),
    ):
        result = await telegram_bot._cmd_update_order("abc1 bogus")

    assert "Estado inválido" in result
    fetch_one.assert_not_awaited()
    update_payment.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_update_order_usage_lists_all_valid_statuses():
    from app.admin import telegram_bot

    result = await telegram_bot._cmd_update_order("")

    assert "Uso: /order ID STATUS" in result
    for status in telegram_bot._VALID_ORDER_STATUSES:
        assert status in result


def test_cmd_update_order_statuses_match_authoritative_order_service():
    from app.admin import telegram_bot
    from app.crm import orders

    expected = orders.VALID_PAYMENT_STATUSES | orders.VALID_SHIPPING_STATUSES
    assert expected == telegram_bot._VALID_ORDER_STATUSES
    assert {"pending", "proof_received", "confirmed", "failed", "rejected"} <= expected
    assert {"pending", "shipped", "delivered"} <= expected
    assert "rejected" in expected


_ORDER_ID = "abc12345-1111-2222-3333-444444444444"


@pytest.mark.parametrize("status", sorted(orders.VALID_PAYMENT_STATUSES))
@pytest.mark.asyncio
async def test_cmd_update_order_routes_each_authoritative_payment_status(status):
    from app.admin import telegram_bot

    update_payment = AsyncMock()
    with (
        patch.object(telegram_bot.db, "fetch_one", AsyncMock(return_value={"id": _ORDER_ID})),
        patch.object(telegram_bot.orders, "update_order_payment_status", update_payment),
    ):
        result = await telegram_bot._cmd_update_order(f"abc1 {status}")

    # The status is Markdown-escaped in the reply, so underscores are doubled.
    assert f"actualizado a *{status.replace('_', '\\_')}*" in result
    update_payment.assert_awaited_once_with(_ORDER_ID, status=status)


@pytest.mark.asyncio
async def test_cmd_update_order_pending_routes_to_payment_status():
    from app.admin import telegram_bot

    # "pending" belongs to both authoritative sets; the command resolves the
    # ambiguity toward payment status (pre-existing /order semantics).
    update_payment = AsyncMock()
    update_shipping = AsyncMock()
    with (
        patch.object(telegram_bot.db, "fetch_one", AsyncMock(return_value={"id": _ORDER_ID})),
        patch.object(telegram_bot.orders, "update_order_payment_status", update_payment),
        patch.object(telegram_bot.orders, "update_order_shipping", update_shipping),
    ):
        await telegram_bot._cmd_update_order("abc1 pending")

    update_payment.assert_awaited_once_with(_ORDER_ID, status="pending")
    update_shipping.assert_not_awaited()


@pytest.mark.parametrize("status", sorted(orders.VALID_SHIPPING_STATUSES - orders.VALID_PAYMENT_STATUSES))
@pytest.mark.asyncio
async def test_cmd_update_order_routes_each_authoritative_shipping_status(status):
    from app.admin import telegram_bot

    update_shipping = AsyncMock()
    with (
        patch.object(telegram_bot.db, "fetch_one", AsyncMock(return_value={"id": _ORDER_ID})),
        patch.object(telegram_bot.orders, "update_order_shipping", update_shipping),
    ):
        result = await telegram_bot._cmd_update_order(f"abc1 {status}")

    assert f"actualizado a *{status.replace('_', '\\_')}*" in result
    update_shipping.assert_awaited_once_with(_ORDER_ID, shipping_status=status)


# -- /customers handler --------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_customers_sorts_by_last_active_desc():
    from app.admin import telegram_bot

    captured_queries = []

    async def fake_fetch_all(query, params=None):
        captured_queries.append(query)
        return []

    with patch.object(telegram_bot.db, "fetch_all", AsyncMock(side_effect=fake_fetch_all)):
        await telegram_bot._cmd_customers("")

    assert len(captured_queries) == 1
    assert "last_active DESC" in captured_queries[0]
    assert "COALESCE(display_name, platform_id) ASC" not in captured_queries[0]


@pytest.mark.asyncio
async def test_cmd_customers_tag_filter_sorts_by_last_active_desc():
    from app.admin import telegram_bot

    captured_queries = []

    async def fake_fetch_all(query, params=None):
        captured_queries.append((query, params))
        return []

    with patch.object(telegram_bot.db, "fetch_all", AsyncMock(side_effect=fake_fetch_all)):
        await telegram_bot._cmd_customers("vip")

    query, params = captured_queries[0]
    assert "last_active DESC" in query
    assert params == {"tag": '["vip"]'}


# -- /settings handler ---------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_settings_is_concise_owner_summary_without_secrets():
    from app.admin import telegram_bot

    settings = {
        "ai_enabled": True,
        "ai_orchestration_mode": "multi_agent",
        "llm_provider": "openai",
        "llm_model": "gpt-5.6-luna",
        "fallback_provider": "anthropic",
        "fallback_model": "claude-haiku-4-5",
        "auto_fallback": True,
        "exchange_rate_reference": "usd_bcv",
        "exchange_rate_usd_bcv": "36.50",
        "store_phone_number": "+584121234567",
        "order_discount_percent": 10.0,
        "order_discount_threshold_usd": 350.0,
        "escalation_telegram_enabled": True,
        "openai_api_key": "sk-should-never-appear",
        "llm_temperature": 0.7,
        "payment_methods": [{"name": "Pago Móvil", "details": "0412..."}],
    }
    config = SimpleNamespace(
        channel_backend="kommo",
        kommo_chats_media_enabled=True,
        kommo_chats_product_images_enabled=False,
        kommo_chats_catalog_pdf_enabled=True,
    )

    with (
        patch.object(telegram_bot.db, "get_settings", AsyncMock(return_value=settings)),
        patch.object(telegram_bot, "get_config", return_value=config),
    ):
        result = await telegram_bot._cmd_settings()

    assert "openai" in result
    assert "gpt-5.6-luna" in result
    assert "multi\\_agent" in result
    assert "36,50" in result
    assert "Backend: kommo" in result
    assert "media en chats" in result
    assert "PDF de catálogo" in result
    assert "openai_api_key" not in result
    assert "sk-should-never-appear" not in result
    assert "llm_temperature" not in result
    assert "payment_methods" not in result
    assert len(result) < 1500


@pytest.mark.asyncio
async def test_cmd_settings_marks_ai_paused_and_missing_rate():
    from app.admin import telegram_bot

    settings = {
        "ai_enabled": False,
        "ai_orchestration_mode": "legacy",
        "llm_provider": "anthropic",
        "llm_model": "claude-haiku-4-5",
        "exchange_rate_reference": "usd_bcv",
        "exchange_rate_usd_bcv": "",
        "escalation_telegram_enabled": False,
    }
    config = SimpleNamespace(
        channel_backend="meta",
        kommo_chats_media_enabled=False,
        kommo_chats_product_images_enabled=False,
        kommo_chats_catalog_pdf_enabled=False,
    )

    with (
        patch.object(telegram_bot.db, "get_settings", AsyncMock(return_value=settings)),
        patch.object(telegram_bot, "get_config", return_value=config),
    ):
        result = await telegram_bot._cmd_settings()

    assert "pausado" in result
    assert "no disponible" in result
    assert "desactivadas" in result
    assert "Backend: meta" in result
    assert "Fallback" not in result
