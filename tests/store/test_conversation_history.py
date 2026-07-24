from unittest.mock import AsyncMock

import pytest
from app.crm.conversations import prepare_history_for_generation


def test_prepare_history_for_generation_keeps_alternating_turns():
    history = [
        {"role": "user", "content": "Tienen pijamas?"},
        {"role": "assistant", "content": "Si, tenemos pijamas disponibles."},
        {"role": "user", "content": "Precio?"},
        {"role": "assistant", "content": "Desde $35."},
    ]

    assert prepare_history_for_generation(history, latest_user_message="Quiero una M") == [
        {"role": "user", "content": "Tienen pijamas?"},
        {"role": "assistant", "content": "Si, tenemos pijamas disponibles."},
        {"role": "user", "content": "Precio?"},
        {"role": "assistant", "content": "Desde $35."},
        {"role": "user", "content": "Quiero una M"},
    ]


def test_prepare_history_for_generation_drops_unanswered_user_runs():
    history = [
        {"role": "user", "content": "Tienen pijamas?"},
        {"role": "user", "content": "Y brasieres?"},
        {"role": "assistant", "content": "Tenemos pijamas y brasieres."},
        {"role": "user", "content": "Precio?"},
        {"role": "user", "content": "Talla M?"},
    ]

    assert prepare_history_for_generation(history, latest_user_message="Me interesa el set negro") == [
        {"role": "user", "content": "Y brasieres?"},
        {"role": "assistant", "content": "Tenemos pijamas y brasieres."},
        {"role": "user", "content": "Me interesa el set negro"},
    ]


@pytest.mark.asyncio
async def test_generate_response_uses_alternating_history_and_strips_later_greeting(monkeypatch):
    from app.ai import engine
    from app.ai.providers.base import LLMResponse

    recorded = {}

    class Provider:
        async def chat(self, **kwargs):
            recorded["messages"] = kwargs["messages"]
            recorded["system_prompt"] = kwargs["system_prompt"]
            return LLMResponse(text="Hola bella, claro, tenemos talla M.", usage={"input_tokens": 1, "output_tokens": 1})

    monkeypatch.setattr(engine.db, "get_settings", AsyncMock(return_value={"ai_enabled": True, "max_conversation_history": 20}))
    monkeypatch.setattr(engine.db, "fetch_one", AsyncMock(return_value={"id": "customer", "conversation_state": "active", "display_name": "Jose"}))
    monkeypatch.setattr(engine.conversations, "get_history", AsyncMock(return_value=[
        {"role": "user", "content": "Tienen pijamas?"},
        {"role": "user", "content": "Y brasieres?"},
        {"role": "assistant", "content": "Tenemos ambas opciones."},
        {"role": "user", "content": "Precio?"},
    ]))
    stored_messages = []

    async def store_message(**kwargs):
        stored_messages.append(kwargs)

    monkeypatch.setattr(engine.conversations, "store_message", AsyncMock(side_effect=store_message))
    monkeypatch.setattr(engine.orders, "get_latest_open_order", AsyncMock(return_value=None))
    monkeypatch.setattr(engine, "get_cached_catalog", lambda: [])
    monkeypatch.setattr(engine, "format_catalog_as_markdown", lambda _catalog: "")
    monkeypatch.setattr(engine, "get_config", lambda: type("Config", (), {"store_name": "Zona Pink"})())
    monkeypatch.setattr(engine, "_list_providers", lambda: ["openai"])
    monkeypatch.setattr(engine, "get_provider", lambda _provider: Provider())
    monkeypatch.setattr(engine.analytics, "log_response", AsyncMock())

    result = await engine.generate_response(
        channel="whatsapp",
        sender_id="sender",
        message_text="Me interesa el set negro",
        customer_id="customer",
    )

    assert recorded["messages"] == [
        {"role": "user", "content": "Y brasieres?"},
        {"role": "assistant", "content": "Tenemos ambas opciones."},
        {"role": "user", "content": "Me interesa el set negro"},
    ]
    assert "Tienen pijamas?" not in str(recorded["messages"])
    assert "No abras con un saludo inicial" in recorded["system_prompt"]
    assert result["text"] == "claro, tenemos talla M."
    assert [message["role"] for message in stored_messages] == ["user", "assistant"]
