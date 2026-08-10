import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _comment_payload(*, comment_id="comment-1", text="Precio?"):
    return {
        "object": "instagram",
        "entry": [{
            "id": "ig-account",
            "time": 1_700_000_000,
            "changes": [{
                "field": "comments",
                "value": {
                    "id": comment_id,
                    "parent_id": "parent-1",
                    "text": text,
                    "from": {"id": "ig-user", "username": "client.one"},
                    "media": {"id": "media-1", "media_product_type": "REELS"},
                },
            }],
        }],
    }


def _story_payload():
    return {
        "object": "instagram",
        "entry": [{
            "id": "ig-account",
            "messaging": [{
                "sender": {"id": "ig-user", "username": "client.one"},
                "timestamp": 1_700_000_000_000,
                "message": {
                    "mid": "mid-story-1",
                    "text": "Precio?",
                    "reply_to": {
                        "story": {
                            "id": "story-1",
                            "url": "https://cdn.example/story.jpg",
                        }
                    },
                },
            }],
        }],
    }


@pytest.mark.asyncio
async def test_normal_instagram_dm_uses_private_message_job(monkeypatch):
    from app.webhooks import instagram

    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(instagram, "enqueue_inbound_message", enqueue)

    await instagram.ingest_instagram_payload({
        "object": "instagram",
        "entry": [{
            "id": "ig-account",
            "messaging": [{
                "sender": {"id": "ig-user"},
                "message": {"mid": "mid-1", "text": "Hola"},
            }],
        }],
    })

    assert enqueue.await_args.kwargs["interaction_type"] == "private_message"
    assert enqueue.await_args.kwargs["integration_context"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attachment_type", "attachment_id", "url", "expected_text"),
    [
        ("image", "image-1", "https://cdn.example/photo.jpg", "una imagen"),
        (
            "audio",
            "voice-1",
            "https://cdn.example/voice.ogg",
            "[Inbound audio:voice-1 pending transcription]",
        ),
    ],
    ids=["image", "voice"],
)
async def test_native_instagram_media_attachment_is_durably_ingested(
    monkeypatch, attachment_type, attachment_id, url, expected_text
):
    from app.webhooks import instagram

    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(instagram, "enqueue_inbound_message", enqueue)

    await instagram.ingest_instagram_payload({
        "object": "instagram",
        "entry": [{
            "id": "ig-account",
            "messaging": [{
                "sender": {"id": "ig-user"},
                "message": {
                    "mid": f"mid-{attachment_id}",
                    "attachments": [{
                        "id": attachment_id,
                        "type": attachment_type,
                        "payload": {"url": url},
                    }],
                },
            }],
        }],
    })

    values = enqueue.await_args.kwargs
    assert expected_text in values["text"]
    assert values["inbound_attachments"] == [{
        "external_message_id": attachment_id,
        "message_type": attachment_type,
        "media_url": url,
    }]
    assert values["media_url"] == (url if attachment_type == "image" else None)


@pytest.mark.asyncio
async def test_public_comment_persists_authoritative_meta_context(monkeypatch):
    from app.webhooks import instagram

    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(instagram, "enqueue_inbound_message", enqueue)
    monkeypatch.setattr(
        instagram,
        "get_config",
        lambda: SimpleNamespace(instagram_account_id="ig-account"),
    )

    await instagram.ingest_instagram_payload(_comment_payload())

    values = enqueue.await_args.kwargs
    assert values["message_id"] == "comment-1"
    assert values["sender_id"] == "ig-user"
    assert values["text"] == "Precio?"
    assert values["interaction_type"] == "instagram_comment"
    assert values["customer_profile"]["instagram_handle"] == "client.one"
    assert values["integration_context"]["provider"] == "meta"
    assert values["integration_context"]["comment_id"] == "comment-1"
    assert values["integration_context"]["parent_comment_id"] == "parent-1"
    assert values["integration_context"]["media_id"] == "media-1"
    assert values["integration_context"]["commenter_id"] == "ig-user"


@pytest.mark.asyncio
async def test_duplicate_comment_receipt_does_not_schedule_processing():
    from app.webhooks import inbound_buffer

    database = SimpleNamespace(transaction=lambda: _Transaction())
    with (
        patch.object(inbound_buffer.db, "get_db", return_value=database),
        patch.object(
            inbound_buffer.db,
            "fetch_one",
            AsyncMock(side_effect=[None, {"id": "receipt-1"}]),
        ),
        patch.object(inbound_buffer.db, "execute", AsyncMock()) as execute,
        patch.object(inbound_buffer.asyncio, "create_task") as create_task,
    ):
        created = await inbound_buffer.enqueue_inbound_message(
            channel="instagram",
            sender_id="ig-user",
            message_id="comment-1",
            text="Precio?",
            interaction_type="instagram_comment",
            integration_context={"comment_id": "comment-1"},
        )

    assert created is False
    execute.assert_not_awaited()
    create_task.assert_not_called()


@pytest.mark.asyncio
async def test_comment_job_is_inserted_separately_with_durable_context():
    from app.webhooks import inbound_buffer

    database = SimpleNamespace(transaction=lambda: _Transaction())
    fetch_one = AsyncMock(side_effect=[None, None, {"id": "job-1"}])
    with (
        patch.object(inbound_buffer.db, "get_db", return_value=database),
        patch.object(inbound_buffer.db, "fetch_one", fetch_one),
        patch.object(inbound_buffer.db, "execute", AsyncMock()),
        patch.object(
            inbound_buffer,
            "get_config",
            return_value=SimpleNamespace(outbound_processing_enabled=False),
        ),
    ):
        created = await inbound_buffer.enqueue_inbound_message(
            channel="instagram",
            sender_id="ig-user",
            message_id="comment-1",
            text="Precio?",
            interaction_type="instagram_comment",
            integration_context={
                "provider": "meta",
                "interaction_type": "instagram_comment",
                "comment_id": "comment-1",
            },
        )

    assert created is True
    insert_query, insert_values = fetch_one.await_args_list[2].args
    assert "INSERT INTO meta_inbound_jobs" in insert_query
    assert insert_values["interaction_type"] == "instagram_comment"
    assert json.loads(insert_values["integration_context"])["comment_id"] == "comment-1"
    assert not any(
        "status = 'pending'" in str(call.args[0])
        for call in fetch_one.await_args_list
    )


class _Transaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_args):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolution", "expected_status", "expected_skus"),
    [
        ({"status": "resolved", "content_id": "content-1", "product_skus": ["SKU-1"]}, "resolved", ["SKU-1"]),
        ({"status": "not_found"}, "not_found", None),
    ],
)
async def test_comment_context_enrichment_applies_product_mapping(
    monkeypatch,
    resolution,
    expected_status,
    expected_skus,
):
    from app.integrations.meta_context import service
    from app.integrations.meta_context.models import MetaMediaDetails

    media = MetaMediaDetails(
        id="media-1",
        permalink="https://www.instagram.com/reel/ABC123/",
        caption="Pijama rosada",
        media_type="VIDEO",
        media_product_type="REELS",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    client = SimpleNamespace(get_media=AsyncMock(return_value=media))
    monkeypatch.setattr(service.MetaContextClient, "from_config", lambda: client)
    monkeypatch.setattr(service, "backfill_instagram_mapping", AsyncMock(return_value={"status": "matched"}))
    monkeypatch.setattr(service, "resolve_content_product_mapping", AsyncMock(return_value=resolution))

    context = await service.enrich_native_instagram_context({
        "provider": "meta",
        "interaction_type": "instagram_comment",
        "comment_id": "comment-1",
        "public_comment_context": {"media_id": "media-1"},
    })

    public = context["public_comment_context"]
    assert public["mapping_status"] == expected_status
    assert public.get("product_skus") == expected_skus
    assert public["post_url"] == "https://www.instagram.com/reel/ABC123/"
    assert public["content_type"] == "reel"


@pytest.mark.asyncio
async def test_story_reply_carries_mapped_product_context(monkeypatch):
    from app.integrations.meta_context import service
    from app.webhooks import instagram

    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(instagram, "enqueue_inbound_message", enqueue)
    await instagram.ingest_instagram_payload(_story_payload())
    initial = enqueue.await_args.kwargs["integration_context"]
    assert enqueue.await_args.kwargs["interaction_type"] == "private_message"
    assert initial["story_id"] == "story-1"

    monkeypatch.setattr(service, "discover_instagram_story", AsyncMock(return_value={"status": "discovered"}))
    monkeypatch.setattr(
        service,
        "resolve_content_product_mapping",
        AsyncMock(return_value={
            "status": "resolved",
            "content_id": "content-1",
            "product_skus": ["SKU-1"],
        }),
    )
    enriched = await service.enrich_native_instagram_context(initial)

    story = enriched["incoming_instagram_context"]
    assert story["source"] == "story_reply"
    assert story["mapping_status"] == "resolved"
    assert story["product_skus"] == ["SKU-1"]
    assert story["selected_product_sku"] == "SKU-1"


def test_ai_story_product_context_accepts_meta_provider():
    from app.ai import engine

    resolved = engine._resolve_private_instagram_content_context(
        channel="instagram",
        integration_context={
            "provider": "meta",
            "interaction_type": "private_message",
            "incoming_instagram_context": {
                "source": "story_reply",
                "story_id": "story-1",
                "mapping_status": "resolved",
                "product_skus": ["SKU-1"],
                "selected_product_sku": "SKU-1",
            },
        },
        message_text="Precio?",
        products=[{
            "sku": "SKU-1",
            "product_name": "Pijama rosada",
            "price": 25,
            "stock": 2,
            "sizes": "S, M",
        }],
    )

    assert resolved["story_id"] == "story-1"
    assert resolved["selected_product_sku"] == "SKU-1"
    assert resolved["products"][0]["name"] == "Pijama rosada"


@pytest.mark.asyncio
async def test_public_comment_response_uses_comment_endpoint_not_dm(monkeypatch):
    from app.webhooks import instagram

    result = {
        "text": "Sí, está disponible.",
        "interactive": None,
        "catalog_pdf": None,
        "product_image": None,
        "customer_id": "customer-1",
        "escalated": False,
        "paused": False,
    }
    monkeypatch.setattr(instagram, "generate_response", AsyncMock(return_value=result))
    send = AsyncMock(return_value={"id": "reply-1"})
    monkeypatch.setattr(instagram, "_send_with_delivery_record", send)
    monkeypatch.setattr(instagram, "_store_delivered_assistant_message", AsyncMock())

    await instagram._deliver_ai_response(
        "ig-user",
        "Hay disponibilidad?",
        inbound_job_id="job-1",
        lease_token="lease-1",
        interaction_type="instagram_comment",
        integration_context={
            "provider": "meta",
            "interaction_type": "instagram_comment",
            "comment_id": "comment-1",
        },
    )

    assert send.await_args.args[0] is instagram.reply_to_comment
    assert send.await_args.kwargs["comment_id"] == "comment-1"
    assert send.await_args.kwargs.get("to") is None


@pytest.mark.asyncio
async def test_paused_instagram_response_sends_and_persists_nothing(monkeypatch):
    from app.webhooks import instagram

    monkeypatch.setattr(
        instagram,
        "generate_response",
        AsyncMock(return_value={"paused": True, "escalated": False, "customer_id": "customer-1"}),
    )
    send = AsyncMock()
    store = AsyncMock()
    monkeypatch.setattr(instagram, "_send_with_delivery_record", send)
    monkeypatch.setattr(instagram, "_store_delivered_assistant_message", store)

    await instagram._deliver_ai_response(
        "ig-user", "Hola", inbound_job_id="job-1", lease_token="lease-1"
    )

    send.assert_not_awaited()
    store.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_outbound_echo_records_manual_takeover(monkeypatch):
    from app.webhooks import instagram

    monkeypatch.setattr(
        instagram,
        "get_config",
        lambda: SimpleNamespace(instagram_account_id="ig-account"),
    )
    monkeypatch.setattr(
        instagram,
        "is_recorded_meta_outbound_message",
        AsyncMock(return_value=False),
    )
    customer = {"id": "customer-1"}
    with (
        patch(
            "app.crm.channel_mappings.resolve_meta_instagram_customer",
            AsyncMock(return_value=customer),
        ),
        patch("app.crm.conversations.store_message", AsyncMock()) as store,
        patch("app.crm.escalations.escalate_customer_manually", AsyncMock()) as escalate,
    ):
        await instagram._process_event({
            "sender": {"id": "ig-account"},
            "recipient": {"id": "ig-user"},
            "message": {"mid": "manual-mid-1", "text": "Te atiendo personalmente", "is_echo": True},
        })

    store.assert_awaited_once_with(
        customer_id="customer-1",
        role="assistant",
        author_type="human",
        content="Te atiendo personalmente",
        channel="instagram",
        source_id="meta-echo:manual-mid-1",
        provider_message_id="manual-mid-1",
        interaction_type="private_message",
    )
    escalate.assert_awaited_once_with("customer-1", channel="instagram")


@pytest.mark.asyncio
async def test_instagram_resume_is_local_and_never_queries_kommo(monkeypatch):
    from app.admin import customer_activation

    monkeypatch.setattr(
        customer_activation,
        "get_config",
        lambda: SimpleNamespace(whatsapp_backend="kommo"),
    )
    get_mapping = AsyncMock()
    activate = AsyncMock(return_value={"id": "customer-1", "conversation_state": "active"})
    monkeypatch.setattr(customer_activation, "get_mapping_by_customer", get_mapping)
    monkeypatch.setattr(
        customer_activation.escalations,
        "mark_customer_active_for_admin",
        activate,
    )

    result = await customer_activation.activate_customer_for_admin({
        "id": "customer-1",
        "channel": "instagram",
    })

    assert result.status == "local_only"
    get_mapping.assert_not_awaited()
    activate.assert_awaited_once_with("customer-1", channel=None)


@pytest.mark.asyncio
async def test_instagram_known_send_failure_retries_safely_and_records_meta_id(monkeypatch):
    from app.channels.meta_errors import MetaSendError
    from app.webhooks import inbound_buffer

    send = AsyncMock(side_effect=[
        MetaSendError("rate limited", retryable=True),
        {"messages": [{"id": "ig-mid-out-1"}]},
    ])
    mark = AsyncMock(return_value=True)
    record = AsyncMock()
    sleep = AsyncMock()
    monkeypatch.setattr(inbound_buffer, "mark_outbound_send_started", mark)
    monkeypatch.setattr(inbound_buffer, "record_outbound_message", record)
    monkeypatch.setattr(inbound_buffer.asyncio, "sleep", sleep)

    response = await inbound_buffer.send_with_delivery_record(
        send, "ig-job-1", "lease-1", to="ig-user", text="Hola"
    )

    assert response == {"messages": [{"id": "ig-mid-out-1"}]}
    assert send.await_count == 2
    sleep.assert_awaited_once_with(1)
    mark.assert_awaited_once_with("ig-job-1", "lease-1")
    record.assert_awaited_once_with("ig-job-1", "lease-1", response)


@pytest.mark.asyncio
async def test_instagram_delivery_failure_is_not_persisted_as_delivered(monkeypatch):
    from app.channels.meta_errors import MetaSendError
    from app.webhooks import instagram

    monkeypatch.setattr(
        instagram,
        "generate_response",
        AsyncMock(return_value={
            "text": "Respuesta",
            "customer_id": "customer-1",
            "paused": False,
            "escalated": False,
        }),
    )
    monkeypatch.setattr(
        instagram,
        "_send_with_delivery_record",
        AsyncMock(side_effect=MetaSendError("rejected", retryable=False)),
    )
    notify = AsyncMock()
    store = AsyncMock()
    monkeypatch.setattr(instagram, "_notify_delivery_failure", notify)
    monkeypatch.setattr(instagram, "_store_delivered_assistant_message", store)

    with pytest.raises(MetaSendError, match="rejected"):
        await instagram._deliver_ai_response(
            "ig-user", "Hola", inbound_job_id="job-1", lease_token="lease-1"
        )

    notify.assert_awaited_once()
    store.assert_not_awaited()


@pytest.mark.asyncio
async def test_instagram_comment_sender_uses_public_replies_endpoint(monkeypatch):
    from app.channels import instagram_sender

    send = AsyncMock(return_value={"id": "reply-1"})
    monkeypatch.setattr(instagram_sender, "_send", send)
    monkeypatch.setattr(
        instagram_sender,
        "get_config",
        lambda: SimpleNamespace(
            instagram_access_token="token",
            meta_graph_api_version="v26.0",
        ),
    )

    await instagram_sender.reply_to_comment("comment-1", "Disponible")

    assert send.await_args.args[0] == "https://graph.facebook.com/v26.0/comment-1/replies"
    assert send.await_args.args[1] == {"message": "Disponible"}


@pytest.mark.asyncio
async def test_instagram_webhook_subscription_includes_comments(monkeypatch):
    from app.channels import instagram_sender

    send = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(instagram_sender, "_send", send)
    monkeypatch.setattr(
        instagram_sender,
        "get_config",
        lambda: SimpleNamespace(
            instagram_access_token="token",
            meta_graph_api_version="v26.0",
        ),
    )

    await instagram_sender.subscribe_page_to_webhooks("page-1")

    assert send.await_args.args[1]["subscribed_fields"] == (
        "messages,messaging_postbacks,comments"
    )


def test_meta_inbound_context_migration_is_new_and_scope_aware():
    migration = Path("store/migrations/016_meta_inbound_instagram_context.sql").read_text(
        encoding="utf-8"
    )

    assert "ADD COLUMN IF NOT EXISTS interaction_type" in migration
    assert "ADD COLUMN IF NOT EXISTS integration_context JSONB" in migration
    assert "instagram_comment" in migration
    assert "DROP INDEX IF EXISTS uq_meta_inbound_jobs_pending_sender" in migration
    assert "(16, 'meta_inbound_instagram_context')" in migration
    runner = Path("store/scripts/migrate.py").read_text(encoding="utf-8")
    assert "016_meta_inbound_instagram_context.sql" in runner
    assert "meta_inbound_instagram_context_migration_sql" in runner


def test_instagram_architecture_is_native_meta_regardless_of_whatsapp_backend():
    from app.config import channel_backend_for

    assert channel_backend_for("instagram", SimpleNamespace(whatsapp_backend="kommo")) == "meta"
    assert channel_backend_for("instagram", SimpleNamespace(whatsapp_backend="meta")) == "meta"

    instagram_source = Path("store/app/webhooks/instagram.py").read_text(encoding="utf-8")
    main_source = Path("store/app/main.py").read_text(encoding="utf-8")
    assert "app.integrations.kommo" not in instagram_source
    assert "from app.webhooks.instagram import router as instagram_router" in main_source
    assert "fastapi_app.include_router(instagram_router)" in main_source


def test_migration_018_drains_only_safe_legacy_work_and_preserves_history():
    migration = Path("store/migrations/018_retire_kommo_instagram.sql").read_text(
        encoding="utf-8"
    )

    assert "WHERE channel = 'instagram'" in migration
    assert "status IN ('pending', 'prepared')" in migration
    assert "salesbot_launched_at IS NULL" in migration
    assert "return_url IS NULL" in migration
    assert "ai_started_at IS NULL" in migration
    assert "RAISE EXCEPTION" in migration
    assert "disable Instagram Kommo ingress" in migration
    assert "'ready', 'processing', 'continuing'" in migration
    assert "status = 'processing'" not in migration
    assert "SET correlation_status = 'expired'" in migration
    assert "WHERE correlation_status IN ('pending', 'ambiguous')" in migration
    assert "instagram_transport_is_meta_native" in migration
    assert "DROP TABLE" not in migration.upper()
    assert "DELETE FROM meta_instagram_context_events" not in migration
    assert "(18, 'retire_kommo_instagram')" in migration
