import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _DBHandle:
    def transaction(self):
        return _Tx()


def _story_payload(*, text="Precio?", sender_id="customer-1", message_id="mid-1"):
    return {
        "object": "instagram",
        "entry": [{
            "id": "ig-account",
            "time": 1_785_500_000,
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


def _story_event(event_id="event-1", *, seconds=0, text="Precio?"):
    now = datetime.now(UTC)
    return {
        "id": event_id,
        "event_type": "story_reply",
        "correlation_status": "pending",
        "matched_kommo_job_id": None,
        "event_timestamp": now + timedelta(seconds=seconds),
        "expires_at": now + timedelta(hours=1),
        "message_text": text,
        "sender_id": "meta-customer",
        "sender_username": "client.one",
        "story_id": "story-1",
        "story_url": "https://cdn.example/story.jpg",
        "media_id": "story-1",
    }


def _private_job(job_id="job-1", *, seconds=1, text="precio?"):
    now = datetime.now(UTC)
    return {
        "id": job_id,
        "status": "waiting_for_context",
        "context_status": "pending",
        "meta_context_event_id": None,
        "interaction_type": "private_message",
        "channel": "instagram",
        "created_at": now + timedelta(seconds=seconds),
        "correlation_timestamp": now + timedelta(seconds=seconds),
        "correlation_timestamp_source": "incoming_message_timestamp",
        "combined_message": text,
        "author_username": "client.one",
        "author_profile_url": None,
        "sender_username": None,
        "sender_profile_url": None,
        "instagram_content_context": {},
    }


def _story_webhook_client(monkeypatch, *, account_id="ig-account"):
    from app.webhooks import meta_instagram_context as webhook

    config = SimpleNamespace(
        meta_instagram_context_enabled=False,
        meta_story_context_enabled=True,
        instagram_verify_token="verify-token",
        meta_app_secret="story-secret",
        instagram_account_id=account_id,
    )
    monkeypatch.setattr(webhook, "get_config", lambda: config)
    app = FastAPI()
    app.include_router(webhook.router)
    return TestClient(app), webhook


def _signature(body):
    return "sha256=" + hmac.new(b"story-secret", body, hashlib.sha256).hexdigest()


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
        lambda event: event["message"].update(deleted=True),
        lambda event: event.update(is_echo=True),
        lambda event: event["sender"].update(id="ig-account"),
    ],
    ids=["normal-dm", "message-echo", "is-deleted", "deleted", "event-echo", "self-sender"],
)
def test_parser_ignores_non_story_or_unsafe_messaging_events(mutate):
    from app.integrations.meta_context.parser import parse_instagram_context_events

    payload = _story_payload()
    mutate(payload["entry"][0]["messaging"][0])

    assert parse_instagram_context_events(payload) == []


def test_signed_story_webhook_validates_account_and_schedules_only_new_event(monkeypatch):
    client, webhook = _story_webhook_client(monkeypatch)
    store = AsyncMock(return_value={"status": "created", "event_id": "event-1"})
    process = AsyncMock()
    monkeypatch.setattr(webhook, "store_context_event", store)
    monkeypatch.setattr(webhook, "process_context_event", process)
    body = json.dumps(_story_payload()).encode()

    response = client.post(
        "/webhooks/meta/instagram-context",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _signature(body)},
    )

    assert response.status_code == 200
    assert response.text == "EVENT_RECEIVED"
    assert store.await_args.args[0].event_type == "story_reply"
    process.assert_awaited_once_with("event-1")

    payload = _story_payload(message_id="mid-other")
    payload["entry"][0]["id"] = "other-account"
    other_body = json.dumps(payload).encode()
    response = client.post(
        "/webhooks/meta/instagram-context",
        content=other_body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _signature(other_body)},
    )
    assert response.status_code == 200
    assert store.await_count == 1


@pytest.mark.parametrize("signature", ["", "sha256=invalid"])
def test_story_webhook_rejects_missing_or_invalid_signature(monkeypatch, signature):
    client, webhook = _story_webhook_client(monkeypatch)
    monkeypatch.setattr(webhook, "store_context_event", AsyncMock())
    body = json.dumps(_story_payload()).encode()

    response = client.post(
        "/webhooks/meta/instagram-context",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )

    assert response.status_code == 403
    webhook.store_context_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_external_meta_story_message_returns_existing_event(monkeypatch):
    from app.integrations.meta_context import service
    from app.integrations.meta_context.models import MetaInstagramContextEvent

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[None, {"id": "existing-event"}])
    monkeypatch.setattr(service, "db", mock_db)
    monkeypatch.setattr(
        service,
        "get_config",
        lambda: SimpleNamespace(meta_context_event_retention_hours=48),
    )
    event = MetaInstagramContextEvent(
        external_event_id="mid-1",
        event_type="story_reply",
        message_id="mid-1",
        sender_id="customer-1",
        message_text="Precio?",
        story_id="story-1",
        media_id="story-1",
    )

    result = await service.store_context_event(event)

    assert result == {"status": "duplicate", "event_id": "existing-event"}
    insert_query, insert_values = mock_db.fetch_one.await_args_list[0].args
    assert "ON CONFLICT DO NOTHING" in insert_query
    assert insert_values["event_type"] == "story_reply"
    assert insert_values["message_id"] == "mid-1"
    duplicate_query = mock_db.fetch_one.await_args_list[1].args[0]
    assert "external_event_id = :external_event_id" in duplicate_query
    assert "message_id = CAST(:message_id AS text)" in duplicate_query


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
@pytest.mark.parametrize("anchor", ["meta", "kommo"])
async def test_story_correlation_works_meta_first_and_kommo_first(monkeypatch, anchor):
    from app.integrations.meta_context import correlation

    event = _story_event()
    job = _private_job()
    monkeypatch.setattr(correlation.db, "get_db", lambda: _DBHandle())
    monkeypatch.setattr(correlation.db, "fetch_one", AsyncMock(return_value={"locked": True}))
    monkeypatch.setattr(correlation, "_load_event", AsyncMock(return_value=event))
    monkeypatch.setattr(correlation, "_load_job", AsyncMock(return_value=job))
    monkeypatch.setattr(correlation, "_candidate_jobs", AsyncMock(return_value=[job]))
    monkeypatch.setattr(correlation, "_candidate_events", AsyncMock(return_value=[event]))
    persist = AsyncMock(return_value=True)
    monkeypatch.setattr(correlation, "_persist_match", persist)
    monkeypatch.setattr(
        correlation,
        "get_config",
        lambda: SimpleNamespace(
            meta_context_match_window_seconds=45,
            meta_story_context_match_window_seconds=45,
        ),
    )

    result = (
        await correlation.correlate_meta_event("event-1")
        if anchor == "meta"
        else await correlation.correlate_kommo_job("job-1")
    )

    assert result["status"] == "matched"
    candidate = persist.await_args.args[0]
    assert candidate.event["id"] == "event-1"
    assert candidate.job["id"] == "job-1"
    assert "text" in candidate.signals


@pytest.mark.asyncio
async def test_duplicate_price_story_replies_remain_ambiguous(monkeypatch):
    from app.integrations.meta_context import correlation

    events = [_story_event("event-1"), _story_event("event-2")]
    job = _private_job()
    monkeypatch.setattr(correlation.db, "get_db", lambda: _DBHandle())
    monkeypatch.setattr(correlation.db, "fetch_one", AsyncMock(return_value={"locked": True}))
    monkeypatch.setattr(correlation, "_load_job", AsyncMock(return_value=job))
    monkeypatch.setattr(correlation, "_candidate_events", AsyncMock(return_value=events))
    mark = AsyncMock()
    monkeypatch.setattr(correlation, "_mark_ambiguous", mark)
    monkeypatch.setattr(
        correlation,
        "get_config",
        lambda: SimpleNamespace(meta_story_context_match_window_seconds=45),
    )

    result = await correlation.correlate_kommo_job("job-1")

    assert result["status"] == "ambiguous"
    assert result["candidate_count"] == 2
    mark.assert_awaited_once()


@pytest.mark.asyncio
async def test_story_candidate_queries_exclude_whatsapp_and_instagram_comments(monkeypatch):
    from app.integrations.meta_context import correlation

    mock_db = MagicMock()
    mock_db.fetch_all = AsyncMock(return_value=[])
    monkeypatch.setattr(correlation, "db", mock_db)
    event = _story_event()

    await correlation._candidate_jobs(event, 45)
    job_query, job_values = mock_db.fetch_all.await_args.args
    assert "job.channel = 'instagram'" in job_query
    assert "job.interaction_type = :interaction_type" in job_query
    assert job_values["interaction_type"] == "private_message"

    await correlation._candidate_events(_private_job(), 45)
    event_query, event_values = mock_db.fetch_all.await_args.args
    assert "event_type = :event_type" in event_query
    assert event_values["event_type"] == "story_reply"


@pytest.mark.asyncio
async def test_story_timeout_releases_private_job_as_normal_dm_and_clears_context(monkeypatch):
    from app.integrations.meta_context import correlation

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value={"id": "job-1"})
    monkeypatch.setattr(correlation, "db", mock_db)

    assert await correlation._release_context_job_if_due("job-1") is True
    query = mock_db.fetch_one.await_args.args[0]
    assert "SET status = 'ready'" in query
    assert "context_status = 'timed_out'" in query
    assert "WHEN interaction_type = 'private_message' THEN '{}'::jsonb" in query
    assert "WHEN interaction_type = 'instagram_comment'" in query


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolution", "expected_status", "expected_skus", "selected"),
    [
        ({"status": "resolved", "content_id": "c1", "product_skus": ["SKU-1"]}, "resolved", ["SKU-1"], "SKU-1"),
        ({"status": "resolved", "content_id": "c1", "product_skus": ["SKU-1", "SKU-2"]}, "resolved", ["SKU-1", "SKU-2"], None),
        ({"status": "ambiguous"}, "ambiguous", [], None),
    ],
    ids=["one-product", "multiple-products", "ambiguous-mapping"],
)
async def test_story_mapping_gate_releases_resolved_and_ambiguous_mappings(
    monkeypatch, resolution, expected_status, expected_skus, selected
):
    from app.integrations.meta_context import service

    event_row = {
        "event_id": "event-1",
        "event_type": "story_reply",
        "media_id": "story-1",
        "story_id": "story-1",
        "story_url": "https://cdn.example/story.jpg",
        "media_permalink": None,
        "media_caption": None,
        "correlation_details": {},
        "job_id": "job-1",
        "public_comment_context": {},
        "instagram_content_context": {"source": "story_reply", "mapping_status": "pending"},
    }
    mock_db = MagicMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    mock_db.fetch_one = AsyncMock(side_effect=[event_row, {"id": "job-1"}])
    monkeypatch.setattr(service, "db", mock_db)
    monkeypatch.setattr(service, "resolve_content_product_mapping", AsyncMock(return_value=resolution))
    monkeypatch.setattr(service, "_record_mapping_status", AsyncMock())

    result = await service.resolve_and_release_matched_job("event-1", force=True)

    assert result["mapping_status"] == expected_status
    context = json.loads(mock_db.fetch_one.await_args_list[1].args[1]["context"])
    assert context["mapping_status"] == expected_status
    assert context.get("product_skus", []) == expected_skus
    assert context.get("selected_product_sku") == selected
    assert "product_sku" not in context


@pytest.mark.asyncio
async def test_unmapped_or_archived_story_stays_waiting_for_mapping_retry(monkeypatch):
    from app.integrations.meta_context import service

    mock_db = MagicMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    mock_db.fetch_one = AsyncMock(return_value={
        "event_id": "event-1",
        "event_type": "story_reply",
        "media_id": "story-1",
        "story_id": "story-1",
        "story_url": None,
        "media_permalink": None,
        "media_caption": None,
        "correlation_details": {},
        "job_id": "job-1",
        "public_comment_context": {},
        "instagram_content_context": {},
    })
    monkeypatch.setattr(service, "db", mock_db)
    # The content resolver intentionally reports archived rows as not found.
    monkeypatch.setattr(
        service,
        "resolve_content_product_mapping",
        AsyncMock(return_value={"status": "not_found"}),
    )
    record_missing = AsyncMock()
    monkeypatch.setattr(service, "_record_mapping_not_found", record_missing)

    result = await service.resolve_and_release_matched_job("event-1", force=True)

    assert result == {"status": "waiting", "mapping_status": "not_found"}
    assert mock_db.fetch_one.await_count == 1
    record_missing.assert_awaited_once_with("event-1")


def _session_context(story_id="story-1", skus=None, selected=None):
    return {
        "source": "story_reply",
        "story_id": story_id,
        "product_skus": skus or ["SKU-1"],
        "selected_product_sku": selected,
        "mapping_status": "resolved",
        "meta_context_event_id": "event-1",
    }


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
    assert json.loads(values["context"])["story_id"] == "story-new"


@pytest.mark.asyncio
async def test_story_session_expiry_or_invalid_mapping_clears_context(monkeypatch):
    from app.crm import sessions

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value={
        "instagram_content_context": _session_context(),
        "instagram_context_expires_at": datetime.now(UTC) - timedelta(seconds=1),
    })
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(sessions, "db", mock_db)

    assert await sessions.load_active_instagram_content_context("customer-1") == {}
    assert "instagram_content_context = '{}'::jsonb" in mock_db.execute.await_args.args[0]


@pytest.mark.asyncio
async def test_archived_story_mapping_clears_active_session_context(monkeypatch):
    from app.crm import sessions

    context = {
        **_session_context(),
        "content_id": "4534aae1-e5b3-47b2-b321-8250a1e20444",
    }
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

    await sessions.store_instagram_content_context(
        "customer-1",
        {**_session_context(), "mapping_status": "ambiguous"},
        ttl_hours=24,
    )
    assert "instagram_content_context = '{}'::jsonb" in mock_db.execute.await_args.args[0]


def _products():
    return [
        {"sku": "SKU-1", "product_name": "Pijama Azul", "price_usd": 25, "sizes": "S, M", "stock": 2},
        {"sku": "SKU-2", "product_name": "Set Rosa", "price_usd": 30, "sizes": "M, L", "stock": 0},
        {"sku": "SKU-3", "product_name": "Bata Negra", "price_usd": 40, "sizes": "U", "stock": 4},
    ]


def test_live_catalog_story_context_supports_one_and_multiple_products():
    from app.ai.engine import _resolve_private_instagram_content_context

    integration = {
        "provider": "kommo",
        "interaction_type": "private_message",
        "current_story_context": True,
        "incoming_instagram_context": _session_context(skus=["SKU-1"]),
    }
    one = _resolve_private_instagram_content_context(
        channel="instagram", integration_context=integration, message_text="Precio?", products=_products()
    )
    assert one["selected_product_sku"] is None
    assert one["products"] == [{
        "sku": "SKU-1",
        "name": "Pijama Azul",
        "price_text": "$25",
        "sizes": "S, M",
        "availability": "disponible",
    }]

    integration["incoming_instagram_context"] = _session_context(skus=["SKU-1", "SKU-2"])
    many = _resolve_private_instagram_content_context(
        channel="instagram", integration_context=integration, message_text="Precio?", products=_products()
    )
    assert many["selected_product_sku"] is None
    assert [product["sku"] for product in many["products"]] == ["SKU-1", "SKU-2"]
    assert many["products"][1]["availability"] == "agotado"


def test_explicit_product_override_and_context_leakage_guards():
    from app.ai.engine import _resolve_private_instagram_content_context

    base = {
        "provider": "kommo",
        "interaction_type": "private_message",
        "current_story_context": False,
        "incoming_instagram_context": _session_context(skus=["SKU-1", "SKU-2"]),
    }
    selected = _resolve_private_instagram_content_context(
        channel="instagram", integration_context=base, message_text="Precio del Set Rosa", products=_products()
    )
    assert selected["selected_product_sku"] == "SKU-2"

    outside_mapping = _resolve_private_instagram_content_context(
        channel="instagram", integration_context=base, message_text="Precio de Bata Negra", products=_products()
    )
    assert outside_mapping == {}

    for channel, interaction_type in [("whatsapp", "private_message"), ("instagram", "instagram_comment")]:
        assert _resolve_private_instagram_content_context(
            channel=channel,
            integration_context={**base, "interaction_type": interaction_type},
            message_text="Precio?",
            products=_products(),
        ) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel", "interaction_type", "expected_status"),
    [
        ("instagram", "private_message", "waiting_for_context"),
        ("instagram", "instagram_comment", "ready"),
        ("whatsapp", "private_message", "ready"),
    ],
)
async def test_salesbot_callback_waits_only_for_instagram_private_story_candidates(
    monkeypatch, channel, interaction_type, expected_status
):
    from app.integrations.kommo import jobs
    from app.integrations.kommo.models import SalesbotWidgetData

    returned_job = {"id": "job-1", "status": expected_status}
    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value=returned_job)
    monkeypatch.setattr(jobs, "db", mock_db)
    monkeypatch.setattr(
        jobs,
        "get_config",
        lambda: SimpleNamespace(
            meta_story_context_enabled=True,
            meta_story_context_wait_seconds=10,
        ),
    )

    result = await jobs.persist_salesbot_callback(
        SalesbotWidgetData(
            lead_id="100",
            origin=channel,
            interaction_type=interaction_type,
        ),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        {"iat": 123456, "entity_type": "leads", "entity_id": "100"},
    )

    query, values = mock_db.fetch_one.await_args.args
    assert result["status"] == expected_status
    assert "job.channel = 'instagram'" in query
    assert "job.interaction_type = 'private_message'" in query
    assert values["story_context_enabled"] is True
    assert values["story_context_wait_seconds"] == 10


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
    assert "Producto seleccionado" in rendered
    assert "Nunca reveles SKUs internos" in rendered
    assert _build_instagram_content_context({"source": "story_reply", "products": []}) == ""


@pytest.mark.asyncio
async def test_story_reply_delivery_continues_salesbot_and_never_sends_through_meta(monkeypatch):
    from app.channels import instagram_sender
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.get_settings = AsyncMock(
        return_value={"ai_enabled": True, "kommo_emoji_mode_instagram": "preserve"}
    )
    mock_db.fetch_one = AsyncMock(side_effect=[
        {"conversation_state": "active"},
        {"id": "job-1"},
        {"assistant_message_persisted_at": None},
        {"id": "job-1"},
    ])
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)
    monkeypatch.setattr(
        jobs,
        "get_config",
        lambda: SimpleNamespace(
            kommo_ai_active_enum_id=1,
            instagram_story_context_ttl_hours=24,
        ),
    )
    monkeypatch.setattr(jobs, "sync_local_state_from_ai_mode", AsyncMock())
    monkeypatch.setattr(
        jobs,
        "evaluate_automation_state",
        lambda **_kwargs: SimpleNamespace(
            allowed=True,
            reason=None,
            needs_ai_mode_initialization=False,
        ),
    )
    monkeypatch.setattr(
        jobs,
        "resolve_customer_from_kommo_job",
        AsyncMock(return_value={"id": "customer-1", "conversation_state": "active"}),
    )
    monkeypatch.setattr(jobs.sessions, "store_instagram_content_context", AsyncMock(
        return_value=_session_context()
    ))
    monkeypatch.setattr(jobs.sessions, "load_active_instagram_content_context", AsyncMock())
    monkeypatch.setattr(
        jobs,
        "generate_response",
        AsyncMock(return_value={"text": "Cuesta $25.", "escalated": False}),
    )
    monkeypatch.setattr(jobs, "upsert_mapping", AsyncMock())
    monkeypatch.setattr(jobs.conversations, "store_message", AsyncMock())
    meta_send = AsyncMock()
    monkeypatch.setattr(instagram_sender, "send_text", meta_send)

    client = MagicMock()
    client.continue_salesbot = AsyncMock(return_value={"accepted": True})
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)

    await jobs._process_ready_job({
        "id": "job-1",
        "processing_lease_id": "00000000-0000-0000-0000-000000000001",
        "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        "combined_message": "Precio?",
        "channel": "instagram",
        "interaction_type": "private_message",
        "instagram_content_context": _session_context(),
        "correlation_id": "corr-1",
    })

    client.continue_salesbot.assert_awaited_once_with(
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        data={"status": "success", "message": "Cuesta $25."},
    )
    meta_send.assert_not_awaited()
    integration_context = jobs.generate_response.await_args.kwargs["integration_context"]
    assert integration_context["incoming_instagram_context"]["story_id"] == "story-1"
    assert integration_context["current_story_context"] is True
