from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_instagram_audio_is_persisted_for_transcription(monkeypatch):
    from app.webhooks import instagram

    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(instagram, "enqueue_inbound_message", enqueue)

    await instagram._process_message(
        "ig-user",
        {
            "mid": "mid-audio",
            "attachments": [{
                "type": "audio",
                "payload": {"url": "https://cdn.example/voice.ogg"},
            }],
        },
        "mid-audio",
        {"identity_provider": "meta"},
    )

    values = enqueue.await_args.kwargs
    assert "pending transcription" in values["text"]
    assert values["media_url"] is None
    assert values["inbound_attachments"] == [{
        "external_message_id": "mid-audio:0",
        "message_type": "audio",
        "media_url": "https://cdn.example/voice.ogg",
    }]


@pytest.mark.asyncio
async def test_instagram_audio_transcription_enters_normal_ai_processor(monkeypatch):
    from app.webhooks import inbound_buffer

    job = {
        "id": "job-1",
        "channel": "instagram",
        "sender_id": "ig-user",
        "message_parts": [inbound_buffer.inbound_audio_placeholder("mid-audio:0")],
        "media_url": "https://cdn.example/voice.ogg",
        "customer_profile": {"identity_provider": "meta"},
        "interaction_type": "private_message",
        "integration_context": {},
        "inbound_attachments": [{
            "external_message_id": "mid-audio:0",
            "message_type": "audio",
            "media_url": "https://cdn.example/voice.ogg",
        }],
        "attempt_count": 1,
        "processing_lease_token": "lease-1",
    }
    processor = AsyncMock()
    transcribe = AsyncMock(return_value="Quiero saber el precio")
    fetch_one = AsyncMock(side_effect=[{"id": "job-1"}, {"id": "job-1"}])
    monkeypatch.setattr(inbound_buffer, "_resolve_processor", lambda _channel: processor)
    monkeypatch.setattr(inbound_buffer, "transcribe_audio_url", transcribe)
    monkeypatch.setattr(inbound_buffer.db, "fetch_one", fetch_one)
    monkeypatch.setattr(inbound_buffer.db, "execute", AsyncMock())

    await inbound_buffer._process_claimed_job(job)

    transcribe.assert_awaited_once_with("https://cdn.example/voice.ogg")
    assert processor.await_args.args[1] == "Quiero saber el precio"
    assert processor.await_args.args[2] is None
    assert processor.await_args.args[7]["inbound_attachments"] == [{
        "type": "audio",
        "external_message_id": "mid-audio:0",
        "transcribed": True,
    }]


@pytest.mark.asyncio
async def test_instagram_transcription_failure_requeues_without_ai(monkeypatch):
    from app.ai.transcription import AudioTranscriptionError
    from app.webhooks import inbound_buffer

    job = {
        "id": "job-1",
        "channel": "instagram",
        "sender_id": "ig-user",
        "message_parts": [inbound_buffer.inbound_audio_placeholder("mid-audio:0")],
        "customer_profile": {},
        "interaction_type": "private_message",
        "integration_context": {},
        "inbound_attachments": [{
            "external_message_id": "mid-audio:0",
            "message_type": "audio",
            "media_url": "https://cdn.example/voice.ogg",
        }],
        "attempt_count": 1,
        "processing_lease_token": "lease-1",
    }
    processor = AsyncMock()
    requeue = AsyncMock()
    error = AudioTranscriptionError("temporarily unavailable", retryable=True)
    monkeypatch.setattr(inbound_buffer, "_resolve_processor", lambda _channel: processor)
    monkeypatch.setattr(inbound_buffer, "transcribe_audio_url", AsyncMock(side_effect=error))
    monkeypatch.setattr(inbound_buffer, "_requeue_failed_job", requeue)

    await inbound_buffer._process_claimed_job(job)

    processor.assert_not_awaited()
    requeue.assert_awaited_once_with(job, error)


@pytest.mark.asyncio
async def test_ai_paused_instagram_inbound_is_stored_without_response(monkeypatch):
    from app.ai import engine

    customer = {
        "id": "customer-1",
        "channel": "instagram",
        "platform_id": "ig-user",
        "conversation_state": "escalated",
        "escalation_source": "manual",
        "is_blocked": False,
    }
    monkeypatch.setattr(engine.db, "get_settings", AsyncMock(return_value={"ai_enabled": True}))
    monkeypatch.setattr(engine.db, "fetch_one", AsyncMock(return_value={"role": "assistant"}))
    monkeypatch.setattr(
        engine,
        "get_config",
        lambda: SimpleNamespace(ai_orchestration_mode="legacy"),
    )
    monkeypatch.setattr(
        "app.crm.channel_mappings.resolve_meta_instagram_customer",
        AsyncMock(return_value=customer),
    )
    store = AsyncMock()
    monkeypatch.setattr(engine.conversations, "store_message", store)
    monkeypatch.setattr(
        engine.escalations,
        "reactivate_if_expired",
        AsyncMock(return_value=SimpleNamespace(status="skipped")),
    )

    result = await engine.generate_response(
        channel="instagram",
        sender_id="ig-user",
        message_text="Hola de nuevo",
        customer_profile={"identity_provider": "meta"},
    )

    assert result["text"] is None
    assert result["escalated"] is True
    store.assert_awaited_once()
    assert store.await_args.kwargs["role"] == "user"


@pytest.mark.asyncio
async def test_backend_ai_echo_does_not_trigger_takeover(monkeypatch):
    from app.webhooks import instagram

    monkeypatch.setattr(
        instagram,
        "is_recorded_meta_outbound_message",
        AsyncMock(return_value=True),
    )
    with (
        patch("app.crm.channel_mappings.resolve_meta_instagram_customer", new=AsyncMock()) as resolve,
        patch("app.crm.escalations.escalate_customer_manually", new=AsyncMock()) as escalate,
    ):
        await instagram._process_event({
            "sender": {"id": "ig-account"},
            "recipient": {"id": "ig-user"},
            "message": {"mid": "ai-mid", "is_echo": True, "text": "Respuesta AI"},
        })

    resolve.assert_not_awaited()
    escalate.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_instagram_echo_is_persisted_and_pauses_ai(monkeypatch):
    from app.webhooks import instagram

    customer = {"id": "customer-1"}
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
    store = AsyncMock()
    monkeypatch.setattr(instagram.conversations, "store_message", store)
    with (
        patch(
            "app.crm.channel_mappings.resolve_meta_instagram_customer",
            new=AsyncMock(return_value=customer),
        ),
        patch("app.crm.escalations.escalate_customer_manually", new=AsyncMock()) as escalate,
    ):
        await instagram._process_event({
            "sender": {"id": "ig-account"},
            "recipient": {"id": "ig-user"},
            "message": {"mid": "human-mid", "is_echo": True, "text": "Te atiendo yo"},
        })

    store.assert_awaited_once_with(
        customer_id="customer-1",
        role="assistant",
        author_type="human",
        content="Te atiendo yo",
        channel="instagram",
        source_id="meta-echo:human-mid",
        provider_message_id="human-mid",
        interaction_type="private_message",
    )
    escalate.assert_awaited_once_with("customer-1", channel="instagram")


@pytest.mark.asyncio
async def test_meta_sender_mapping_reuses_existing_customer(monkeypatch):
    from app.crm import channel_mappings

    mapping = {"customer_id": "customer-1"}
    customer = {
        "id": "customer-1",
        "channel": "instagram",
        "platform_id": "legacy-id",
    }
    monkeypatch.setattr(
        channel_mappings,
        "lookup_by_provider",
        AsyncMock(return_value=mapping),
    )
    monkeypatch.setattr(channel_mappings.db, "fetch_one", AsyncMock(return_value=customer))
    get_customer = AsyncMock(return_value=customer)
    monkeypatch.setattr(channel_mappings.customers, "get_or_create_customer", get_customer)
    persist = AsyncMock()
    monkeypatch.setattr(channel_mappings, "persist_verified_meta_instagram_sender", persist)

    first = await channel_mappings.resolve_meta_instagram_customer("ig-user")
    second = await channel_mappings.resolve_meta_instagram_customer("ig-user")

    assert first["id"] == second["id"] == "customer-1"
    assert get_customer.await_count == 2
    persist.assert_not_awaited()


def test_native_instagram_automatic_escalation_never_expires(monkeypatch):
    from app.crm import escalations

    monkeypatch.setattr(
        escalations,
        "get_config",
        lambda: SimpleNamespace(whatsapp_backend="kommo", instagram_backend="meta"),
    )
    customer = {
        "conversation_state": "escalated",
        "escalation_source": "automatic",
        "escalation_expires_at": datetime.now(UTC) - timedelta(minutes=1),
        "channel": "instagram",
        "is_blocked": False,
    }

    assert escalations._quick_skip_reason(customer, datetime.now(UTC)) == (
        "native_instagram_manual_resume_required"
    )


@pytest.mark.asyncio
async def test_new_native_instagram_automatic_escalation_has_no_expiry(monkeypatch):
    from app.crm import escalations

    fetch_one = AsyncMock(return_value={"id": "customer-1"})
    monkeypatch.setattr(escalations.db, "fetch_one", fetch_one)
    monkeypatch.setattr(
        escalations,
        "get_config",
        lambda: SimpleNamespace(whatsapp_backend="kommo", instagram_backend="meta"),
    )

    await escalations.escalate_customer_automatically(
        "customer-1",
        settings={"automatic_escalation_timeout_minutes": 180},
        channel="instagram",
    )

    assert fetch_one.await_args.args[1]["expires_at"] is None


def test_operations_migration_adds_audio_and_human_provenance():
    from pathlib import Path

    migration = Path("store/migrations/017_meta_instagram_operations.sql").read_text(
        encoding="utf-8"
    )
    assert "inbound_attachments JSONB" in migration
    assert "author_type TEXT" in migration
    assert "provider_message_id TEXT" in migration
    assert "(17, 'meta_instagram_operations')" in migration
