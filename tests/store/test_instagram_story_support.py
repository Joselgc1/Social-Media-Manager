import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _story_payload(*, text="Precio?", sender_id="customer-1", message_id="mid-1"):
    return {
        "object": "instagram",
        "entry": [{
            "id": "ig-account",
            "messaging": [{
                "sender": {"id": sender_id, "username": "Client.One"},
                "timestamp": 1_785_500_123_000,
                "message": {
                    "mid": message_id,
                    "text": text,
                    "reply_to": {
                        "story": {
                            "id": "story-1",
                            "url": "https://cdn.example/story.jpg?temporary=1",
                        }
                    },
                },
            }],
        }],
    }


def _session_context(story_id="story-1", skus=None, selected=None):
    return {
        "source": "story_reply",
        "story_id": story_id,
        "product_skus": skus or ["SKU-1"],
        "selected_product_sku": selected,
        "mapping_status": "resolved",
        "meta_context_event_id": "event-1",
    }


def _products():
    return [
        {"sku": "SKU-1", "product_name": "Pijama Azul", "price_usd": 25, "sizes": "S, M", "stock": 2},
        {"sku": "SKU-2", "product_name": "Set Rosa", "price_usd": 30, "sizes": "M, L", "stock": 0},
        {"sku": "SKU-3", "product_name": "Bata Negra", "price_usd": 40, "sizes": "U", "stock": 4},
    ]


def test_parser_accepts_valid_story_reply_with_stable_identifiers():
    from app.integrations.meta_context.parser import parse_instagram_context_events

    events = parse_instagram_context_events(_story_payload())

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "story_reply"
    assert event.external_event_id == event.message_id == "mid-1"
    assert event.instagram_account_id == "ig-account"
    assert event.sender_id == "customer-1"
    assert event.sender_username == "Client.One"
    assert event.message_text == "Precio?"
    assert event.story_id == event.media_id == "story-1"
    assert event.story_url == "https://cdn.example/story.jpg?temporary=1"
    assert event.event_timestamp == datetime.fromtimestamp(1_785_500_123, tz=UTC)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event["message"].pop("reply_to"),
        lambda event: event["message"].update(is_echo=True),
        lambda event: event["message"].update(is_deleted=True),
        lambda event: event.update(is_self=True),
        lambda event: event["sender"].update(id="ig-account"),
    ],
    ids=["normal-dm", "echo", "deleted", "self", "account-sender"],
)
def test_parser_ignores_non_story_or_unsafe_messaging_events(mutate):
    from app.integrations.meta_context.parser import parse_instagram_context_events

    payload = _story_payload()
    mutate(payload["entry"][0]["messaging"][0])

    assert parse_instagram_context_events(payload) == []


@pytest.mark.asyncio
async def test_story_discovery_is_idempotent_and_uses_stable_id_not_cdn_url(monkeypatch):
    from app.integrations.meta_context import service

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value={"id": "content-1", "status": "active"})
    monkeypatch.setattr(service, "db", mock_db)
    monkeypatch.setattr(
        service,
        "get_config",
        lambda: SimpleNamespace(instagram_story_mapping_ttl_hours=24),
    )
    discovered_at = datetime(2026, 8, 3, 10, tzinfo=UTC)

    first = await service.discover_instagram_story({
        "story_id": "story-1",
        "story_url": "https://cdn.example/first.jpg",
        "event_timestamp": discovered_at,
    })
    second = await service.discover_instagram_story({
        "story_id": "story-1",
        "story_url": "https://cdn.example/rotated.jpg",
        "event_timestamp": discovered_at + timedelta(minutes=5),
    })

    assert first == second == {"status": "discovered", "content_id": "content-1"}
    query = mock_db.fetch_one.await_args_list[0].args[0]
    assert "ON CONFLICT (media_id) WHERE media_id IS NOT NULL DO UPDATE" in query
    assert "WHERE instagram_content.content_type = 'story'" in query
    assert all(call.args[1]["media_id"] == "story-1" for call in mock_db.fetch_one.await_args_list)
    assert mock_db.fetch_one.await_args_list[1].args[1]["thumbnail_url"].endswith("rotated.jpg")


@pytest.mark.asyncio
async def test_story_session_store_replaces_context_and_loads_active_value(monkeypatch):
    from app.crm import sessions

    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    context = _session_context(story_id="story-new", skus=["SKU-2"])
    mock_db.fetch_one = AsyncMock(return_value={
        "instagram_content_context": json.dumps(context),
        "instagram_context_expires_at": datetime.now(UTC) + timedelta(hours=1),
    })
    monkeypatch.setattr(sessions, "db", mock_db)

    stored = await sessions.store_instagram_content_context("customer-1", context, ttl_hours=24)
    loaded = await sessions.load_active_instagram_content_context("customer-1")

    assert stored == loaded
    query, values = mock_db.execute.await_args.args
    assert "ON CONFLICT (customer_id) DO UPDATE" in query
    assert "instagram_content_context = CAST(:context AS jsonb)" in query
    assert values["ttl_hours"] == 24


@pytest.mark.asyncio
async def test_archived_story_mapping_clears_active_session_context(monkeypatch):
    from app.crm import sessions

    context = {**_session_context(), "content_id": "4534aae1-e5b3-47b2-b321-8250a1e20444"}
    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value={
        "instagram_content_context": context,
        "instagram_context_expires_at": datetime.now(UTC) + timedelta(hours=1),
    })
    mock_db.fetch_all = AsyncMock(return_value=[])
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(sessions, "db", mock_db)

    assert await sessions.load_active_instagram_content_context("customer-1") == {}
    assert "content.status = 'active'" in mock_db.fetch_all.await_args.args[0]
    assert "instagram_content_context = '{}'::jsonb" in mock_db.execute.await_args.args[0]


def test_live_catalog_story_context_supports_one_and_multiple_products():
    from app.ai.engine import _resolve_private_instagram_content_context

    integration = {
        "provider": "meta",
        "interaction_type": "private_message",
        "current_story_context": True,
        "incoming_instagram_context": _session_context(skus=["SKU-1"]),
    }
    one = _resolve_private_instagram_content_context(
        channel="instagram", integration_context=integration, message_text="Precio?", products=_products()
    )
    assert one["products"][0] == {
        "sku": "SKU-1",
        "name": "Pijama Azul",
        "price_text": "$25",
        "sizes": "S, M",
        "availability": "disponible",
    }

    integration["incoming_instagram_context"] = _session_context(skus=["SKU-1", "SKU-2"])
    many = _resolve_private_instagram_content_context(
        channel="instagram", integration_context=integration, message_text="Precio?", products=_products()
    )
    assert [product["sku"] for product in many["products"]] == ["SKU-1", "SKU-2"]
    assert many["products"][1]["availability"] == "agotado"


def test_multi_product_story_selection_and_context_leakage_guards():
    from app.ai.engine import _resolve_private_instagram_content_context

    integration = {
        "provider": "meta",
        "interaction_type": "private_message",
        "current_story_context": True,
        "incoming_instagram_context": _session_context(skus=["SKU-1", "SKU-2"]),
    }
    selected = _resolve_private_instagram_content_context(
        channel="instagram",
        integration_context=integration,
        message_text="Me interesa el Set Rosa",
        products=_products(),
    )
    assert selected["selected_product_sku"] == "SKU-2"

    outside_mapping = _resolve_private_instagram_content_context(
        channel="instagram",
        integration_context=integration,
        message_text="Precio de Bata Negra",
        products=_products(),
    )
    assert outside_mapping == {"_clear_story_context": True}
    assert _resolve_private_instagram_content_context(
        channel="instagram",
        integration_context={**integration, "interaction_type": "instagram_comment"},
        message_text="Precio?",
        products=_products(),
    ) == {}


def test_story_prompt_uses_live_context_without_internal_or_unmapped_leakage():
    from app.ai.prompts import _build_instagram_content_context

    rendered = _build_instagram_content_context({
        "source": "story_reply",
        "selected_product_sku": "SKU-1",
        "products": [{
            "sku": "SKU-1",
            "name": "Pijama Azul",
            "price_text": "$25.00",
            "sizes": "S, M",
            "availability": "disponible",
        }],
    })
    assert "Pijama Azul" in rendered
    assert "$25.00" in rendered
    assert "Nunca reveles SKUs internos" in rendered
    assert _build_instagram_content_context({"source": "story_reply", "products": []}) == ""
