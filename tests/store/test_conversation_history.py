from unittest.mock import AsyncMock

import pytest
from app.crm import conversations
from app.crm.conversations import prepare_history_for_generation


class _ScopedHistoryDB:
    def __init__(self):
        self.rows = []

    async def execute(self, _query, values):
        self.rows.append(
            {
                "customer_id": values["cid"],
                "role": values["role"],
                "content": values["content"],
                "attachments": None,
                "interaction_type": values["interaction_type"],
            }
        )

    async def fetch_all(self, _query, values):
        matching = [
            row
            for row in self.rows
            if row["customer_id"] == values["cid"]
            and row["interaction_type"] == values["interaction_type"]
        ]
        return list(reversed(matching[-values["limit"] :]))


async def _store_turn(
    customer_id: str,
    user_text: str,
    assistant_text: str,
    *,
    channel: str = "instagram",
    interaction_type: str = "private_message",
):
    await conversations.store_message(
        customer_id,
        "user",
        user_text,
        channel,
        interaction_type=interaction_type,
    )
    await conversations.store_message(
        customer_id,
        "assistant",
        assistant_text,
        channel,
        interaction_type=interaction_type,
    )


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
    assert "interaction_type" in query
    assert values["interaction_type"] == "private_message"
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
async def test_store_message_persists_instagram_comment_scope(monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr(conversations.db, "execute", execute)

    await conversations.store_message(
        "customer",
        "user",
        "Precio?",
        "instagram",
        interaction_type="instagram_comment",
    )

    assert execute.await_args.args[1]["interaction_type"] == "instagram_comment"


@pytest.mark.asyncio
@pytest.mark.parametrize("interaction_type", ["private_message", "instagram_comment"])
async def test_get_history_filters_by_interaction_scope(monkeypatch, interaction_type):
    fetch_all = AsyncMock(return_value=[])
    monkeypatch.setattr(conversations.db, "fetch_all", fetch_all)

    await conversations.get_history(
        "customer",
        limit=20,
        interaction_type=interaction_type,
    )

    query, values = fetch_all.await_args.args
    assert "interaction_type = :interaction_type" in query
    assert values["interaction_type"] == interaction_type


@pytest.mark.asyncio
async def test_get_recent_summary_filters_by_interaction_scope(monkeypatch):
    fetch_all = AsyncMock(return_value=[])
    monkeypatch.setattr(conversations.db, "fetch_all", fetch_all)

    await conversations.get_recent_summary(
        "customer",
        interaction_type="instagram_comment",
    )

    query, values = fetch_all.await_args.args
    assert "interaction_type = :interaction_type" in query
    assert values["interaction_type"] == "instagram_comment"


@pytest.mark.asyncio
async def test_dm_comment_dm_history_excludes_public_comment_turn(monkeypatch):
    history_db = _ScopedHistoryDB()
    monkeypatch.setattr(conversations, "db", history_db)

    await _store_turn("customer", "Información de productos", "Tenemos varios perfumes")
    await _store_turn(
        "customer",
        "Precio?",
        "Gucci cuesta $71",
        interaction_type="instagram_comment",
    )

    dm_history = await conversations.get_history("customer", interaction_type="private_message")

    assert dm_history == [
        {"role": "user", "content": "Información de productos"},
        {"role": "assistant", "content": "Tenemos varios perfumes"},
    ]
    assert "Gucci" not in str(dm_history)


@pytest.mark.asyncio
async def test_comment_then_dm_history_excludes_comment_and_reply(monkeypatch):
    history_db = _ScopedHistoryDB()
    monkeypatch.setattr(conversations, "db", history_db)

    await _store_turn(
        "customer",
        "Precio?",
        "El precio es $71",
        interaction_type="instagram_comment",
    )

    assert await conversations.get_history("customer", interaction_type="private_message") == []


@pytest.mark.asyncio
async def test_dm_then_comment_history_excludes_private_dm(monkeypatch):
    history_db = _ScopedHistoryDB()
    monkeypatch.setattr(conversations, "db", history_db)

    await _store_turn("customer", "Quiero comprar", "Claro, te ayudo")

    assert await conversations.get_history(
        "customer",
        interaction_type="instagram_comment",
    ) == []


@pytest.mark.asyncio
async def test_consecutive_public_comments_keep_comment_follow_up_context(monkeypatch):
    history_db = _ScopedHistoryDB()
    monkeypatch.setattr(conversations, "db", history_db)

    await _store_turn(
        "customer",
        "Precio?",
        "El precio es $71",
        interaction_type="instagram_comment",
    )

    comment_history = await conversations.get_history(
        "customer",
        interaction_type="instagram_comment",
    )
    assert comment_history[-2:] == [
        {"role": "user", "content": "Precio?"},
        {"role": "assistant", "content": "El precio es $71"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["instagram", "whatsapp"])
async def test_private_message_continuity_remains_default_for_dm_and_whatsapp(monkeypatch, channel):
    history_db = _ScopedHistoryDB()
    monkeypatch.setattr(conversations, "db", history_db)

    await _store_turn("customer", "Hola", "Hola bella", channel=channel)

    assert await conversations.get_history("customer") == [
        {"role": "user", "content": "Hola"},
        {"role": "assistant", "content": "Hola bella"},
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
    engine.conversations.get_history.assert_awaited_once_with(
        "customer",
        limit=20,
        interaction_type="private_message",
    )
    assert {message["interaction_type"] for message in stored_messages} == {"private_message"}
