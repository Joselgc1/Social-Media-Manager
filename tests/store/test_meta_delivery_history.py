from unittest.mock import AsyncMock, patch

import pytest


def _response(text="Respuesta enviada"):
    return {
        "text": text,
        "interactive": None,
        "catalog_pdf": None,
        "product_image": None,
        "customer_id": "customer-1",
        "escalated": False,
        "function_calls": [{"name": "check_inventory"}],
    }


@pytest.mark.asyncio
async def test_whatsapp_assistant_history_is_written_after_successful_send():
    from app.webhooks import whatsapp

    events = []

    async def send_success(**kwargs):
        events.append(("send", kwargs["text"]))

    async def store_success(**kwargs):
        events.append(("store", kwargs["content"]))

    generate = AsyncMock(return_value=_response())
    with (
        patch.object(whatsapp, "generate_response", generate),
        patch.object(whatsapp, "send_text", side_effect=send_success),
        patch.object(whatsapp.conversations, "store_message", side_effect=store_success),
    ):
        await whatsapp._deliver_ai_response("584121234567", "Hola", None, {}, "meta-job-1")

    assert events == [("send", "Respuesta enviada"), ("store", "Respuesta enviada")]
    assert generate.await_args.kwargs["persist_assistant_message"] is False
    assert generate.await_args.kwargs["persist_user_before_response"] is True
    assert generate.await_args.kwargs["message_source_id"] == "meta-job-1"


@pytest.mark.asyncio
async def test_whatsapp_delivery_failure_does_not_create_assistant_history():
    from app.webhooks import whatsapp

    store_message = AsyncMock()
    with (
        patch.object(whatsapp, "generate_response", AsyncMock(return_value=_response())),
        patch.object(whatsapp, "send_text", AsyncMock(side_effect=RuntimeError("graph unavailable"))),
        patch.object(whatsapp, "_notify_delivery_failure", AsyncMock()),
        patch.object(whatsapp.conversations, "store_message", store_message),
        pytest.raises(RuntimeError, match="graph unavailable"),
    ):
        await whatsapp._deliver_ai_response("584121234567", "Hola", None, {}, "meta-job-2")

    store_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_whatsapp_partial_delivery_stores_only_successful_parts():
    from app.webhooks import whatsapp

    result = _response("Aquí está el catálogo")
    result["catalog_pdf"] = {"type": "catalog_pdf", "caption": "Catálogo"}
    store_message = AsyncMock()
    with (
        patch.object(whatsapp, "generate_response", AsyncMock(return_value=result)),
        patch.object(whatsapp, "send_document", AsyncMock()),
        patch.object(whatsapp, "send_text", AsyncMock(side_effect=RuntimeError("text failed"))),
        patch.object(whatsapp, "_notify_delivery_failure", AsyncMock()),
        patch.object(whatsapp.conversations, "store_message", store_message),
        patch.object(whatsapp, "get_config", return_value=type("Config", (), {"app_base_url": "https://store.test"})()),
        pytest.raises(RuntimeError, match="text failed"),
    ):
        await whatsapp._deliver_ai_response("584121234567", "Catálogo", None, {}, "meta-job-partial")

    store_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_instagram_assistant_history_is_written_only_after_send():
    from app.webhooks import instagram

    events = []

    async def send_success(**kwargs):
        events.append(("send", kwargs["text"]))

    async def store_success(**kwargs):
        events.append(("store", kwargs["content"]))

    with (
        patch.object(instagram, "generate_response", AsyncMock(return_value=_response("Respuesta IG"))),
        patch.object(instagram, "send_text", side_effect=send_success),
        patch.object(instagram.conversations, "store_message", side_effect=store_success),
    ):
        await instagram._deliver_ai_response("ig-user", "Hola", None, {}, "meta-job-3")

    assert events == [("send", "Respuesta IG"), ("store", "Respuesta IG")]


@pytest.mark.asyncio
async def test_instagram_long_unpunctuated_response_is_delivered_without_truncation():
    from app.webhooks import instagram

    response_text = "á" * 1001
    send_text = AsyncMock()
    store_message = AsyncMock()
    with (
        patch.object(instagram, "generate_response", AsyncMock(return_value=_response(response_text))),
        patch.object(instagram, "send_text", send_text),
        patch.object(instagram.conversations, "store_message", store_message),
    ):
        await instagram._deliver_ai_response("ig-user", "Hola", None, {}, "meta-job-long")

    chunks = [call.kwargs["text"] for call in send_text.await_args_list]
    assert len(chunks) == 3
    assert all(len(chunk.encode("utf-8")) <= 950 for chunk in chunks)
    assert "".join(chunks) == response_text
    assert store_message.await_args.kwargs["content"] == "\n".join(chunks)


@pytest.mark.asyncio
async def test_conversation_source_id_makes_delivery_persistence_idempotent():
    from app.crm import conversations

    execute = AsyncMock()
    with patch.object(conversations.db, "execute", execute):
        await conversations.store_message(
            customer_id="customer-1",
            role="assistant",
            content="Entregado",
            channel="whatsapp",
            source_id="meta-job-1",
        )

    query, values = execute.await_args.args
    assert "ON CONFLICT (channel, role, source_id)" in query
    assert values["source_id"] == "meta-job-1"
