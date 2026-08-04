from unittest.mock import AsyncMock

import pytest
from app.crm import conversations
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
async def test_store_message_persists_only_semantic_attachments(monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr(conversations.db, "execute", execute)

    await conversations.store_message(
        customer_id="customer",
        role="assistant",
        content="Aquí lo tienes.",
        channel="whatsapp",
        attachments=[
            {
                "type": "product_image",
                "product_name": "Coconut Passion",
                "sku": "VS-CP-01",
                "drive_uuid": "must-not-be-stored",
                "source_url": "https://example.com/private",
            }
        ],
    )

    query, values = execute.await_args.args
    assert "CAST(:attachments AS jsonb)" in query
    assert values["attachments"] == (
        '[{"type": "product_image", "product_name": "Coconut Passion", "sku": "VS-CP-01"}]'
    )
    assert "drive_uuid" not in values["attachments"]
    assert "source_url" not in values["attachments"]


@pytest.mark.asyncio
async def test_get_history_keeps_legacy_messages_without_attachments(monkeypatch):
    monkeypatch.setattr(
        conversations.db,
        "fetch_all",
        AsyncMock(
            return_value=[
                {"role": "assistant", "content": "Respuesta anterior", "attachments": None},
                {"role": "user", "content": "Hola"},
            ]
        ),
    )

    assert await conversations.get_history("customer") == [
        {"role": "user", "content": "Hola"},
        {"role": "assistant", "content": "Respuesta anterior"},
    ]


@pytest.mark.asyncio
async def test_get_history_adds_product_image_context_without_transport_data(monkeypatch):
    monkeypatch.setattr(
        conversations.db,
        "fetch_all",
        AsyncMock(
            return_value=[
                {
                    "role": "assistant",
                    "content": "Claro 💕 Aquí lo tienes.",
                    "attachments": [
                        {
                            "type": "product_image",
                            "product_name": "Coconut Passion",
                            "sku": "VS-CP-01",
                            "drive_uuid": "hidden",
                        }
                    ],
                }
            ]
        ),
    )

    history = await conversations.get_history("customer")

    assert history[0]["content"] == (
        'Claro 💕 Aquí lo tienes.\n\n[Contexto de entrega: Eva también envió una imagen '
        'del producto "Coconut Passion", SKU VS-CP-01.]'
    )
    assert "drive_uuid" not in history[0]["content"]
    assert "hidden" not in history[0]["content"]


@pytest.mark.asyncio
async def test_get_history_adds_catalog_pdf_context_without_internal_metadata(monkeypatch):
    monkeypatch.setattr(
        conversations.db,
        "fetch_all",
        AsyncMock(
            return_value=[
                {
                    "role": "assistant",
                    "content": "Aquí tienes nuestro catálogo actualizado 💕",
                    "attachments": '[{"type":"catalog_pdf","filename":"Catalogo Zona Pink.pdf",'
                    '"catalog_fingerprint":"secret-fingerprint"}]',
                }
            ]
        ),
    )

    history = await conversations.get_history("customer")

    assert history[0]["content"].endswith(
        "[Contexto de entrega: Eva envió el catálogo PDF actualizado.]"
    )
    assert "secret-fingerprint" not in history[0]["content"]
    assert "Catalogo Zona Pink.pdf" not in history[0]["content"]


@pytest.mark.asyncio
async def test_attachment_only_assistant_turn_produces_non_empty_model_history(monkeypatch):
    monkeypatch.setattr(
        conversations.db,
        "fetch_all",
        AsyncMock(
            return_value=[
                {
                    "role": "assistant",
                    "content": "",
                    "attachments": [
                        {
                            "type": "product_image",
                            "product_name": "Coconut Passion",
                            "sku": "VS-CP-01",
                        }
                    ],
                },
                {"role": "user", "content": "Muéstrame Coconut Passion", "attachments": None},
            ]
        ),
    )

    history = await conversations.get_history("customer")
    prepared = prepare_history_for_generation(history)

    assert prepared == [
        {"role": "user", "content": "Muéstrame Coconut Passion"},
        {
            "role": "assistant",
            "content": (
                '[Contexto de entrega: Eva también envió una imagen del producto '
                '"Coconut Passion", SKU VS-CP-01.]'
            ),
        },
    ]


@pytest.mark.asyncio
async def test_repeated_kommo_job_persistence_keeps_one_row_per_role(monkeypatch):
    logical_rows = set()

    async def execute(query, values):
        assert "ON CONFLICT (channel, role, source_id)" in query
        logical_rows.add((values["channel"], values["role"], values["source_id"]))

    monkeypatch.setattr(conversations.db, "execute", execute)
    source_id = "kommo-job:11111111-1111-4111-8111-111111111111"
    for _ in range(2):
        await conversations.store_message(
            "customer",
            "user",
            "Hola",
            "whatsapp",
            source_id=source_id,
        )
        await conversations.store_message(
            "customer",
            "assistant",
            "Hola bella",
            "whatsapp",
            source_id=source_id,
            function_calls=[{"name": "check_inventory"}],
        )

    assert logical_rows == {
        ("whatsapp", "user", source_id),
        ("whatsapp", "assistant", source_id),
    }

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
