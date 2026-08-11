import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.integrations.kommo.client import KommoAPIError
from app.integrations.kommo.delivery import KommoDeliveryAbortedError, KommoDeliveryUnknownError
from app.integrations.kommo.models import NormalizedKommoEvent, SalesbotWidgetData
from app.integrations.kommo.state import evaluate_automation_state

LEASE_ID = "00000000-0000-0000-0000-000000000001"


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _DBHandle:
    def transaction(self):
        return _Tx()


def _install_ready_job_db(monkeypatch, jobs, *, settings=None, assistant_persisted_at=None):
    mock_db = MagicMock()
    mock_db.get_settings = AsyncMock(return_value=settings or {"ai_enabled": True})
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    async def fetch_one(query, values=None):
        if "SELECT conversation_state FROM customers" in query:
            return {"conversation_state": "active"}
        if "SELECT assistant_message_persisted_at" in query:
            return {"assistant_message_persisted_at": assistant_persisted_at}
        return {"id": "job"}

    mock_db.fetch_one = AsyncMock(side_effect=fetch_one)
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)
    monkeypatch.setattr(jobs.conversations, "store_message", AsyncMock())
    return mock_db


def _install_direct_ready_job(
    monkeypatch,
    jobs,
    *,
    ai_result=None,
    delivery_result=None,
    delivery_error=None,
    customer_send_begun=False,
):
    from app.integrations.kommo.delivery import DeliveryResult

    mock_db = _install_ready_job_db(
        monkeypatch,
        jobs,
        settings={"ai_enabled": True, "kommo_emoji_mode_instagram": "preserve"},
    )
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
        jobs.sessions,
        "load_active_instagram_content_context",
        AsyncMock(return_value={}),
    )
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
        AsyncMock(return_value={"id": "customer", "conversation_state": "active"}),
    )
    generate = AsyncMock(
        return_value=ai_result or {"text": "Respuesta directa", "escalated": False}
    )
    monkeypatch.setattr(jobs, "generate_response", generate)
    monkeypatch.setattr(jobs, "upsert_mapping", AsyncMock())
    monkeypatch.setattr(
        jobs,
        "_direct_customer_send_begun",
        AsyncMock(return_value=customer_send_begun),
    )
    store_message = AsyncMock()
    monkeypatch.setattr(jobs.conversations, "store_message", store_message)
    deliver = AsyncMock(
        return_value=delivery_result
        or DeliveryResult(
            transport="chats_api",
            customer_text="Respuesta directa",
            delivered_attachments=[],
            provider_message_ids=["instagram-message"],
        ),
        side_effect=delivery_error,
    )
    monkeypatch.setattr(jobs, "deliver_response", deliver)
    client = MagicMock()
    client.run_salesbot = AsyncMock()
    client.continue_salesbot = AsyncMock()
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)
    return mock_db, client, generate, deliver, store_message


def _bind_names(query: str) -> set[str]:
    return set(re.findall(r":([A-Za-z_][A-Za-z0-9_]*)", query))


def _continuation_values(mock_db) -> dict:
    return next(
        call.args[1]
        for call in mock_db.fetch_one.await_args_list
        if "continuation_payload" in call.args[1]
    )


class _StrictCallbackDB:
    def __init__(self, *, update_job=None, latest_job=None):
        self.update_job = update_job
        self.latest_job = latest_job
        self.calls = []

    async def fetch_one(self, query, values=None):
        supplied = set((values or {}).keys())
        expected = _bind_names(query)
        assert supplied == expected
        self.calls.append((query, dict(values or {})))
        if "UPDATE kommo_message_jobs job" in query:
            return self.update_job
        if "SELECT * FROM kommo_message_jobs" in query:
            return self.latest_job
        return None


class _CallbackCreateDB:
    def __init__(self, *, duplicate_job=None, duplicate_is_stale=False, superseded_private_jobs=None):
        self.duplicate_job = duplicate_job
        self.duplicate_is_stale = duplicate_is_stale
        self.superseded_private_jobs = [
            {
                "timestamp_delta_seconds": 0.0,
                "private_timestamp_source": "receipt_received_at",
                "comment_timestamp_source": "salesbot_iat",
                **row,
            }
            for row in (superseded_private_jobs or [])
        ]
        self.fetch_one_calls = []
        self.fetch_all_calls = []
        self.execute_calls = []

    def get_db(self):
        return _DBHandle()

    async def execute(self, query, values=None):
        self.execute_calls.append((query, dict(values or {})))
        return None

    async def fetch_one(self, query, values=None):
        self.fetch_one_calls.append((query, dict(values or {})))
        if "SET status = 'discarded'" in query:
            return {"id": (values or {}).get("job_id")}
        if "UPDATE kommo_message_jobs job" in query:
            return None
        if "INSERT INTO kommo_message_jobs" in query:
            return {"id": "comment-job", "status": "ready", **dict(values or {})}
        if "FROM kommo_message_jobs" in query:
            if (
                self.duplicate_is_stale
                and self.duplicate_job
                and (values or {}).get("external_message_id") != self.duplicate_job.get("external_message_id")
            ):
                return None
            return self.duplicate_job
        return None

    async def fetch_all(self, query, values=None):
        self.fetch_all_calls.append((query, dict(values or {})))
        if "FROM kommo_message_jobs private_job" in query and "interaction_type = 'private_message'" in query:
            return self.superseded_private_jobs
        return []


class _LaunchSafetyDB:
    def __init__(self, *, comment_match=None):
        self.comment_match = comment_match
        self.direct_prepared_status = None
        self.fetch_one_calls = []
        self.execute_calls = []

    def get_db(self):
        return _DBHandle()

    async def fetch_one(self, query, values=None):
        supplied = set((values or {}).keys())
        expected = _bind_names(query)
        assert supplied == expected
        self.fetch_one_calls.append((query, dict(values or {})))
        if "comment_job.interaction_type = 'instagram_comment'" in query:
            return self.comment_match
        if "SET status = 'discarded'" in query:
            return {"id": (values or {}).get("job_id")} if self.comment_match else None
        if "status = 'waiting_for_salesbot'" in query:
            return {"id": (values or {}).get("id"), "status": "waiting_for_salesbot", "lead_id": "100", "contact_id": "200", "channel": "instagram", "combined_message": "Precio?"}
        if "SET status = CASE WHEN :wait_for_story_context" in query:
            self.direct_prepared_status = "waiting_for_context" if (values or {}).get("wait_for_story_context") else "ready"
            return {
                "id": (values or {}).get("id"),
                "status": self.direct_prepared_status,
                "lead_id": "100",
                "contact_id": "200",
                "talk_id": "300",
                "channel": "instagram",
                "interaction_type": "private_message",
                "combined_message": "Precio?",
            }
        return None

    async def execute(self, query, values=None):
        self.execute_calls.append((query, dict(values or {})))
        return None


def test_ai_state_decisions():
    config = SimpleNamespace(
        kommo_ai_active_enum_id=1,
        kommo_ai_human_enum_id=2,
        kommo_ai_paused_enum_id=3,
    )
    assert evaluate_automation_state(
        ai_enabled=True,
        local_conversation_state="active",
        kommo_ai_mode_enum_id=1,
        config=config,
    ).allowed is True
    assert evaluate_automation_state(
        ai_enabled=False,
        local_conversation_state="active",
        kommo_ai_mode_enum_id=1,
        config=config,
    ).reason == "global_ai_paused"
    assert evaluate_automation_state(
        ai_enabled=True,
        local_conversation_state="escalated",
        kommo_ai_mode_enum_id=1,
        config=config,
    ).reason == "local_escalated"
    assert evaluate_automation_state(
        ai_enabled=True,
        local_conversation_state="blocked",
        kommo_ai_mode_enum_id=1,
        config=config,
    ).reason == "local_blocked"
    assert evaluate_automation_state(
        ai_enabled=True,
        local_conversation_state="active",
        kommo_ai_mode_enum_id=2,
        config=config,
    ).reason == "kommo_human_mode"
    assert evaluate_automation_state(
        ai_enabled=True,
        local_conversation_state="active",
        kommo_ai_mode_enum_id=3,
        config=config,
    ).reason == "kommo_paused_mode"
    empty = evaluate_automation_state(
        ai_enabled=True,
        local_conversation_state="active",
        kommo_ai_mode_enum_id=None,
        config=config,
    )
    assert empty.needs_ai_mode_initialization is True


@pytest.mark.asyncio
async def test_ai_mode_initialization_refetches_before_writing_active(monkeypatch):
    from app.integrations.kommo import state

    monkeypatch.setattr(
        state,
        "get_config",
        lambda: SimpleNamespace(
            kommo_ai_mode_field_id=10,
            kommo_ai_active_enum_id=1,
            kommo_ai_human_enum_id=2,
            kommo_ai_paused_enum_id=3,
        ),
    )
    client = MagicMock()
    client.get_lead = AsyncMock(return_value={
        "custom_fields_values": [{"field_id": 10, "values": [{"enum_id": 2}]}]
    })
    client.update_ai_mode = AsyncMock()

    enum_id, initialized_now = await state.ensure_ai_mode_initialized(
        client,
        "123",
        {"custom_fields_values": []},
    )

    assert enum_id == 2
    assert initialized_now is False
    client.get_lead.assert_awaited_once_with("123")
    client.update_ai_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_persistent_job_creation_and_duplicate_prevention(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[None, None, None])
    mock_db.execute = AsyncMock(return_value="job-id")
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    monkeypatch.setattr(jobs, "db", mock_db)

    event = NormalizedKommoEvent(
        event_type="incoming_message",
        message_id="m1",
        lead_id="100",
        contact_id="200",
        chat_id="chat",
        text="Hola",
        origin="whatsapp",
        channel="whatsapp",
        author_id="author-1",
        author_name="Maria Cliente",
        author_username="@maria.cliente",
        author_profile_url="https://instagram.com/maria.cliente",
        sender_username="sender.profile",
        sender_profile_url="https://instagram.com/sender.profile",
        author_type="external",
    )
    result = await jobs.record_incoming_event(event)
    assert result == {"status": "created", "job_id": "job-id"}
    assert "pg_advisory_xact_lock" in mock_db.fetch_one.await_args_list[0].args[0]
    assert "FOR UPDATE" in mock_db.fetch_one.await_args_list[2].args[0]
    assert mock_db.execute.await_count == 2
    assert "INSERT INTO kommo_message_jobs" in mock_db.execute.await_args_list[0].args[0]
    assert mock_db.execute.await_args_list[0].args[1]["author_id"] == "author-1"
    assert mock_db.execute.await_args_list[0].args[1]["author_name"] == "Maria Cliente"
    assert mock_db.execute.await_args_list[0].args[1]["author_username"] == "@maria.cliente"
    assert mock_db.execute.await_args_list[0].args[1]["author_profile_url"] == "https://instagram.com/maria.cliente"
    assert mock_db.execute.await_args_list[0].args[1]["sender_username"] == "sender.profile"
    assert mock_db.execute.await_args_list[0].args[1]["sender_profile_url"] == "https://instagram.com/sender.profile"
    assert mock_db.execute.await_args_list[0].args[1]["interaction_type"] == "private_message"
    assert "INSERT INTO kommo_message_receipts" in mock_db.execute.await_args_list[1].args[0]
    created_receipt = mock_db.execute.await_args_list[1].args[1]
    assert created_receipt["author_id"] == "author-1"
    assert created_receipt["interaction_type"] == "private_message"
    assert created_receipt["message_text"] == "Hola"
    assert created_receipt["normalized_text_hash"] == (
        "b221d9dbb083a7f33428d7c2a3c3198ae925614d70210e28716ccaa7cd4ddb79"
    )

    mock_db.fetch_one = AsyncMock(side_effect=[None, {"job_id": "existing", "receipt_status": "created"}])
    duplicate = await jobs.record_incoming_event(event)
    assert duplicate == {"status": "duplicate", "job_id": "existing"}


@pytest.mark.asyncio
async def test_voice_job_creation_persists_ordered_attachment_and_stable_placeholder(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[None, None, None])
    mock_db.execute = AsyncMock(return_value="voice-job")
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    monkeypatch.setattr(jobs, "db", mock_db)

    await jobs.record_incoming_event(NormalizedKommoEvent(
        event_type="incoming_message",
        message_id="voice-1",
        chat_id="chat",
        message_type="voice",
        media_url="https://media.example/voice-1.ogg?signature=secret",
        origin="whatsapp",
        channel="whatsapp",
    ))

    _, values = mock_db.execute.await_args_list[0].args
    assert values["combined_message"] == "[Kommo voice:voice-1 pendiente de transcripcion]"
    assert json.loads(values["inbound_attachments"]) == [{
        "external_message_id": "voice-1",
        "message_type": "voice",
        "media_url": "https://media.example/voice-1.ogg?signature=secret",
    }]


@pytest.mark.asyncio
async def test_debounce_merges_rapid_messages(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[None, None, {
        "id": "pending-id",
        "combined_message": "Hola",
        "media_url": None,
        "message_type": None,
        "inbound_attachments": [],
    }])
    mock_db.execute = AsyncMock(side_effect=[None, "discarded-id"])
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    monkeypatch.setattr(jobs, "db", mock_db)

    event = NormalizedKommoEvent(
        event_type="incoming_message",
        message_id="m2",
        lead_id="100",
        text="Tienen pijamas?",
        origin="whatsapp",
        channel="whatsapp",
        author_id="author-new",
        author_name="Maria Nueva",
        author_username="@maria.nueva",
        sender_profile_url="https://instagram.com/sender.nueva",
    )
    result = await jobs.record_incoming_event(event)
    assert result["status"] == "merged"
    update_call = mock_db.execute.await_args_list[0]
    assert update_call.args[1]["combined_message"] == "Hola\nTienen pijamas?"
    assert update_call.args[1]["interaction_type"] == "private_message"
    assert "author_id = COALESCE(:author_id, author_id)" in update_call.args[0]
    assert "author_name = COALESCE(:author_name, author_name)" in update_call.args[0]
    assert "author_username = COALESCE(:author_username, author_username)" in update_call.args[0]
    assert "sender_profile_url = COALESCE(:sender_profile_url, sender_profile_url)" in update_call.args[0]
    assert "lead_id = COALESCE(lead_id, :lead_id)" in update_call.args[0]
    assert "interaction_type = :interaction_type" in update_call.args[0]
    assert update_call.args[1]["author_id"] == "author-new"
    assert update_call.args[1]["author_name"] == "Maria Nueva"
    assert update_call.args[1]["author_username"] == "@maria.nueva"
    assert update_call.args[1]["sender_profile_url"] == "https://instagram.com/sender.nueva"
    receipt_call = mock_db.execute.await_args_list[1]
    assert "INSERT INTO kommo_message_receipts" in receipt_call.args[0]
    assert receipt_call.args[1]["job_id"] == "pending-id"
    assert receipt_call.args[1]["receipt_status"] == "merged"
    assert receipt_call.args[1]["author_id"] == "author-new"
    assert receipt_call.args[1]["interaction_type"] == "private_message"
    assert receipt_call.args[1]["message_text"] == "Tienen pijamas?"
    assert receipt_call.args[1]["normalized_text_hash"] == (
        "e50b75a2d85c9da8c1dbcd477b2d73f4f9f1e50c78121c72bf526255b968cbdc"
    )
    assert update_call.args[1]["combined_message"] == "Hola\nTienen pijamas?"
    assert all("'discarded'" not in call.args[0] for call in mock_db.execute.await_args_list)


@pytest.mark.asyncio
async def test_debounce_voice_then_image_keeps_legacy_media_fields_consistent(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[None, None, {
        "id": "pending-id",
        "combined_message": jobs._audio_placeholder("voice", "voice-1"),
        "media_url": "https://media.example/voice-1.ogg",
        "message_type": "voice",
        "inbound_attachments": [{
            "external_message_id": "voice-1",
            "message_type": "voice",
            "media_url": "https://media.example/voice-1.ogg",
        }],
    }])
    mock_db.execute = AsyncMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    monkeypatch.setattr(jobs, "db", mock_db)

    result = await jobs.record_incoming_event(NormalizedKommoEvent(
        event_type="incoming_message",
        message_id="image-1",
        chat_id="chat",
        message_type="image",
        media_url="https://media.example/image-1.jpg",
        origin="whatsapp",
        channel="whatsapp",
    ))

    assert result["status"] == "merged"
    query, values = mock_db.execute.await_args_list[0].args
    assert values["media_url"] == "https://media.example/image-1.jpg"
    assert values["message_type"] == "image"
    assert json.loads(values["inbound_attachments"]) == [{
        "external_message_id": "image-1",
        "message_type": "image",
        "media_url": "https://media.example/image-1.jpg",
    }]
    assert "attachment ->> 'external_message_id' = :external_message_id" in query


@pytest.mark.asyncio
async def test_duplicate_voice_webhook_does_not_append_attachment(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[
        None,
        {"job_id": "existing-job", "receipt_status": "created"},
    ])
    mock_db.execute = AsyncMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    monkeypatch.setattr(jobs, "db", mock_db)
    event = NormalizedKommoEvent(
        event_type="incoming_message",
        message_id="voice-duplicate",
        chat_id="chat",
        message_type="voice",
        media_url="https://media.example/voice.ogg?signature=secret",
        origin="whatsapp",
        channel="whatsapp",
    )

    result = await jobs.record_incoming_event(event)

    assert result == {"status": "duplicate", "job_id": "existing-job"}
    mock_db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_image_in_mixed_media_job_does_not_append_attachment(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[
        None,
        {"job_id": "mixed-job", "receipt_status": "merged"},
    ])
    mock_db.execute = AsyncMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    monkeypatch.setattr(jobs, "db", mock_db)

    result = await jobs.record_incoming_event(NormalizedKommoEvent(
        event_type="incoming_message",
        message_id="image-duplicate",
        chat_id="chat",
        message_type="picture",
        media_url="https://media.example/image.jpg?signature=secret",
        origin="whatsapp",
        channel="whatsapp",
    ))

    assert result == {"status": "duplicate", "job_id": "mixed-job"}
    mock_db.execute.assert_not_awaited()


def test_comment_and_private_messages_have_separate_job_correlation():
    private_event = NormalizedKommoEvent(
        event_type="incoming_message",
        message_id="dm-1",
        lead_id="100",
        text="Hola por DM",
        origin="instagram",
        channel="instagram",
        interaction_type="private_message",
    )
    comment_event = NormalizedKommoEvent(
        event_type="incoming_message",
        message_id="comment-1",
        lead_id="100",
        text="Precio?",
        origin="instagram",
        channel="instagram",
        interaction_type="instagram_comment",
    )

    assert private_event.correlation_id == "kommo:private_message:100"
    assert comment_event.correlation_id == "kommo:instagram_comment:100"
    assert private_event.correlation_id != comment_event.correlation_id


def test_private_messages_on_same_lead_use_specific_chat_correlation():
    first_chat = NormalizedKommoEvent(
        event_type="incoming_message",
        message_id="dm-1",
        lead_id="100",
        contact_id="200",
        chat_id="chat-a",
        text="Hola",
        channel="whatsapp",
    )
    second_chat = NormalizedKommoEvent(
        event_type="incoming_message",
        message_id="dm-2",
        lead_id="100",
        contact_id="201",
        chat_id="chat-b",
        text="Hola",
        channel="instagram",
    )

    assert first_chat.correlation_id == "kommo:private_message:chat-a"
    assert second_chat.correlation_id == "kommo:private_message:chat-b"
    assert first_chat.correlation_id != second_chat.correlation_id


@pytest.mark.asyncio
async def test_atomic_job_claiming_prevents_concurrent_salesbot_runs(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value=None)
    monkeypatch.setattr(jobs, "db", mock_db)
    await jobs._claim_due_pending_job()
    query = mock_db.fetch_one.await_args.args[0]
    assert "FOR UPDATE SKIP LOCKED" in query
    assert "NOT EXISTS" in query
    assert "waiting_for_salesbot" in query
    assert "active.status IN ('prepared', 'waiting_for_salesbot', 'waiting_for_context', 'waiting_for_delivery', 'ready', 'processing', 'continuing')" in query
    assert "active.lead_id = candidate.lead_id" in query
    assert "active.contact_id = candidate.contact_id" in query
    assert "pg_try_advisory_xact_lock" in query


@pytest.mark.asyncio
async def test_duplicate_callback_prevention(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[None, {"id": "job", "status": "sent"}])
    monkeypatch.setattr(jobs, "db", mock_db)

    result = await jobs.persist_salesbot_callback(
        SalesbotWidgetData(lead_id="100"),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        {
            "jti": "token-id",
            "iat": 123456,
            "account_id": 123,
            "user_id": 456,
            "client_uid": "client-uuid",
            "subdomain": "acme",
            "entity_type": "leads",
            "entity_id": "100",
        },
    )
    assert result == {"status": "duplicate", "job_id": "job"}
    claim_query = mock_db.fetch_one.await_args_list[0].args[0]
    claim_values = mock_db.fetch_one.await_args_list[0].args[1]
    assert "FOR UPDATE SKIP LOCKED" in claim_query
    assert "callback_claims" in claim_query
    assert "candidate.lead_id = :entity_id" in claim_query
    assert "candidate.salesbot_launched_at < to_timestamp" in claim_query
    assert ":salesbot_token_iat_text" in claim_query
    assert "consumed.return_url = :return_url" in claim_query
    assert "CAST(:interaction_type AS text)" in claim_query
    assert claim_values["salesbot_token_jti"] == "token-id"
    assert claim_values["entity_id"] == "100"
    assert claim_values["interaction_type"] == "private_message"
    assert claim_values["expected_channel"] is None
    assert "lead_id" not in claim_values
    assert "contact_id" not in claim_values
    assert "return_url = :return_url" in mock_db.fetch_one.await_args_list[1].args[0]
    assert "ORDER BY salesbot_launched_at" not in mock_db.fetch_one.await_args_list[1].args[0]
    fallback_values = mock_db.fetch_one.await_args_list[1].args[1]
    assert fallback_values == {
        "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        "salesbot_token_jti": "token-id",
        "salesbot_token_iat_text": "123456",
    }


@pytest.mark.asyncio
async def test_salesbot_callback_uses_signed_lead_identity(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value={"id": "job", "status": "waiting_for_salesbot"})
    monkeypatch.setattr(jobs, "db", mock_db)

    result = await jobs.persist_salesbot_callback(
        SalesbotWidgetData(contact_id="200"),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        {"iat": 123456, "account_id": 123, "client_uid": "client-uuid", "entity_type": "leads", "entity_id": "100"},
    )
    query = mock_db.fetch_one.await_args.args[0]
    values = mock_db.fetch_one.await_args.args[1]
    assert result == {"status": "ready", "job_id": "job"}
    assert "candidate.lead_id = :entity_id" in query
    assert "CAST(:interaction_type AS text)" in query
    assert values["entity_type"] == "leads"
    assert values["entity_id"] == "100"
    assert values["interaction_type"] == "private_message"
    assert values["expected_channel"] is None
    assert "lead_id" not in values
    assert "contact_id" not in values


@pytest.mark.asyncio
async def test_salesbot_callback_uses_signed_contact_identity(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value={"id": "job", "status": "waiting_for_salesbot"})
    monkeypatch.setattr(jobs, "db", mock_db)

    result = await jobs.persist_salesbot_callback(
        SalesbotWidgetData(lead_id="100"),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        {"iat": 123456, "account_id": 123, "client_uid": "client-uuid", "entity_type": "contacts", "entity_id": "200"},
    )
    query = mock_db.fetch_one.await_args.args[0]
    values = mock_db.fetch_one.await_args.args[1]
    assert result == {"status": "ready", "job_id": "job"}
    assert "candidate.contact_id = :entity_id" in query
    assert values["entity_type"] == "contacts"
    assert values["entity_id"] == "200"
    assert values["interaction_type"] == "private_message"
    assert values["expected_channel"] is None
    assert "lead_id" not in values
    assert "contact_id" not in values


@pytest.mark.asyncio
async def test_comment_salesbot_callback_matches_existing_waiting_comment_job(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value={"id": "comment-job", "status": "waiting_for_salesbot", "interaction_type": "instagram_comment"})
    monkeypatch.setattr(jobs, "db", mock_db)

    result = await jobs.persist_salesbot_callback(
        SalesbotWidgetData(lead_id="100", interaction_type="instagram_comment"),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        {"iat": 123456, "account_id": 123, "client_uid": "client-uuid", "entity_type": "leads", "entity_id": "100"},
    )

    query = mock_db.fetch_one.await_args.args[0]
    values = mock_db.fetch_one.await_args.args[1]
    assert result == {"status": "ready", "job_id": "comment-job"}
    assert "candidate.interaction_type = CAST(:interaction_type AS text)" in query
    assert values["interaction_type"] == "instagram_comment"


@pytest.mark.asyncio
async def test_native_comment_callback_creates_ready_job_from_signed_identity(monkeypatch, caplog):
    from app.integrations.kommo import jobs

    create_db = _CallbackCreateDB()
    monkeypatch.setattr(jobs, "db", create_db)

    with caplog.at_level("INFO", logger="app.integrations.kommo.jobs"):
        result = await jobs.persist_salesbot_callback(
            SalesbotWidgetData(
                message="Precio?",
                lead_id="100",
                contact_id="spoofed-contact",
                origin="instagram",
                interaction_type="instagram_comment",
                post_id="{{post.id}}",
                post_caption="Nueva Pijama satén azul disponible",
                product_sku="PJ-001",
            ),
            "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
            {
                "jti": "token-id",
                "iat": 123456,
                "account_id": 123,
                "user_id": 456,
                "client_uid": "client-uuid",
                "entity_type": "leads",
                "entity_id": "100",
            },
        )

    assert result == {"status": "ready", "job_id": "comment-job"}
    update_query, update_values = create_db.fetch_one_calls[0]
    assert "candidate.status = 'waiting_for_salesbot'" in update_query
    assert "candidate.interaction_type = CAST(:interaction_type AS text)" in update_query
    assert update_values["interaction_type"] == "instagram_comment"

    insert_query, insert_values = next(call for call in create_db.fetch_one_calls if "INSERT INTO kommo_message_jobs" in call[0])
    assert "status, buffer_expires_at" in insert_query
    assert insert_values["lead_id"] == "100"
    assert insert_values["contact_id"] is None
    assert insert_values["channel"] == "instagram"
    assert insert_values["interaction_type"] == "instagram_comment"
    assert insert_values["combined_message"] == "Precio?"
    assert json.loads(insert_values["public_comment_context"]) == {
        "post_caption": "Nueva Pijama satén azul disponible",
        "product_sku": "PJ-001",
    }
    assert insert_values["salesbot_token_jti"] == "token-id"
    assert insert_values["external_message_id"].startswith("kommo:instagram_comment_callback:")
    assert insert_values["correlation_id"].startswith("kommo:instagram_comment:")
    assert "pg_advisory_xact_lock(hashtext(:dedupe_key))" in create_db.execute_calls[0][0]
    assert "pg_advisory_xact_lock(hashtext(:reconciliation_key))" in create_db.execute_calls[0][0]
    reconciliation_query, reconciliation_values = create_db.fetch_all_calls[0]
    assert "'processing', 'waiting_for_salesbot'" in reconciliation_query
    assert "continuation_payload IS NULL" in reconciliation_query
    assert "COALESCE(receipt.received_at, receipt.created_at, private_job.created_at)" in reconciliation_query
    assert "COALESCE(meta_event.event_timestamp, to_timestamp(:comment_event_timestamp))" in reconciliation_query
    assert reconciliation_values["window_seconds"] == jobs.COMMENT_MIRROR_RECONCILIATION_SECONDS
    assert reconciliation_values["comment_event_timestamp"] == 123456
    assert reconciliation_values["normalized_message"] == "precio?"
    assert "INSERT INTO kommo_message_receipts" in create_db.execute_calls[1][0]
    assert create_db.execute_calls[1][1]["job_id"] == "comment-job"
    assert "ready job creation started" in caplog.text
    assert "message_text_resolved=True" in caplog.text
    assert "context_keys=['post_caption', 'product_sku']" in caplog.text
    assert "signed_entity_type=leads" in caplog.text
    assert "signed_entity_id=100" in caplog.text
    assert "created comment job" in caplog.text
    assert "Precio?" not in caplog.text


@pytest.mark.asyncio
async def test_native_comment_callback_duplicate_uses_jti_or_stable_callback(monkeypatch):
    from app.integrations.kommo import jobs

    create_db = _CallbackCreateDB(duplicate_job={"id": "existing-comment", "status": "sent", "interaction_type": "instagram_comment"})
    monkeypatch.setattr(jobs, "db", create_db)

    result = await jobs.persist_salesbot_callback(
        SalesbotWidgetData(message="Precio?", lead_id="100", origin="instagram", interaction_type="instagram_comment"),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        {
            "jti": "token-id",
            "iat": 123456,
            "account_id": 123,
            "client_uid": "client-uuid",
            "entity_type": "leads",
            "entity_id": "100",
        },
    )

    assert result == {"status": "duplicate", "job_id": "existing-comment"}
    duplicate_query, duplicate_values = create_db.fetch_one_calls[1]
    assert "salesbot_token_jti = CAST(:salesbot_token_jti AS text)" in duplicate_query
    assert "callback_claims ->> 'iat'" in duplicate_query
    assert "return_url = :return_url" in duplicate_query
    assert duplicate_values["salesbot_token_jti"] == "token-id"
    assert not any("INSERT INTO kommo_message_jobs" in query for query, _values in create_db.fetch_one_calls)
    assert not create_db.execute_calls


@pytest.mark.asyncio
async def test_comment_callback_token_replay_is_duplicate_even_with_altered_text(monkeypatch):
    from app.integrations.kommo import jobs

    create_db = _CallbackCreateDB(duplicate_job={"id": "existing-comment", "status": "sent", "interaction_type": "instagram_comment"})
    monkeypatch.setattr(jobs, "db", create_db)

    result = await jobs.persist_salesbot_callback(
        SalesbotWidgetData(message="Texto alterado", lead_id="100", origin="instagram", interaction_type="instagram_comment"),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        {
            "jti": "token-id",
            "iat": 123456,
            "account_id": 123,
            "client_uid": "client-uuid",
            "entity_type": "leads",
            "entity_id": "100",
        },
    )

    assert result == {"status": "duplicate", "job_id": "existing-comment"}
    duplicate_query, duplicate_values = create_db.fetch_one_calls[1]
    assert "salesbot_token_jti = CAST(:salesbot_token_jti AS text)" in duplicate_query
    assert "_normalized_message_sql" not in duplicate_query
    assert duplicate_values["salesbot_token_iat_text"] == "123456"
    assert not any("INSERT INTO kommo_message_jobs" in query for query, _values in create_db.fetch_one_calls)


@pytest.mark.asyncio
async def test_private_webhook_job_created_before_comment_callback_is_discarded(monkeypatch):
    from app.integrations.kommo import jobs

    create_db = _CallbackCreateDB(superseded_private_jobs=[{"id": "private-job"}])
    monkeypatch.setattr(jobs, "db", create_db)

    result = await jobs.persist_salesbot_callback(
        SalesbotWidgetData(message="  Precio?  ", lead_id="100", origin="instagram", interaction_type="instagram_comment"),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        {
            "jti": "token-id",
            "iat": 123456,
            "account_id": 123,
            "client_uid": "client-uuid",
            "entity_type": "leads",
            "entity_id": "100",
        },
    )

    assert result == {"status": "ready", "job_id": "comment-job"}
    reconciliation_query, reconciliation_values = create_db.fetch_all_calls[0]
    assert "interaction_type = 'private_message'" in reconciliation_query
    assert "channel = 'instagram'" in reconciliation_query
    assert "'processing', 'waiting_for_salesbot'" in reconciliation_query
    assert "continuation_payload IS NULL" in reconciliation_query
    assert "FOR UPDATE OF private_job SKIP LOCKED" in reconciliation_query
    assert reconciliation_values["normalized_message"] == "precio?"
    assert reconciliation_values["window_seconds"] == jobs.COMMENT_MIRROR_RECONCILIATION_SECONDS
    assert reconciliation_values["comment_event_timestamp"] == 123456
    discard_values = next(
        values
        for query, values in create_db.fetch_one_calls
        if "SET status = 'discarded'" in query
    )
    assert discard_values["reason"] == "superseded_by_instagram_comment"


@pytest.mark.asyncio
async def test_comment_callback_reconciles_private_job_already_processing(monkeypatch):
    from app.integrations.kommo import jobs

    create_db = _CallbackCreateDB(superseded_private_jobs=[{"id": "processing-private-job"}])
    monkeypatch.setattr(jobs, "db", create_db)

    count = await jobs._discard_recent_private_jobs_superseded_by_comment_callback(
        values={"entity_type": "leads", "entity_id": "100"},
        normalized_message="precioo",
        comment_event_timestamp=1_786_023_182,
    )

    assert count == 1
    query, values = create_db.fetch_all_calls[0]
    assert "'processing', 'waiting_for_salesbot'" in query
    assert "'waiting_for_context', 'ready'" in query
    assert "continuation_payload IS NULL" in query
    assert "assistant_message_persisted_at IS NULL" in query
    assert "receipt.received_at" in query
    assert "meta_event.event_timestamp" in query
    assert "created_at >= NOW()" not in query
    assert values["comment_event_timestamp"] == 1_786_023_182
    discard_query = next(
        query for query, _values in create_db.fetch_one_calls if "SET status = 'discarded'" in query
    )
    assert "processing_lease_id = NULL" in discard_query
    assert "NOT EXISTS" in discard_query
    assert "outbound.status IN ('sending', 'accepted', 'confirmed', 'delivery_unknown')" in discard_query


@pytest.mark.asyncio
async def test_comment_reconciliation_does_not_discard_after_direct_fence(monkeypatch):
    from app.integrations.kommo import jobs

    create_db = _CallbackCreateDB(superseded_private_jobs=[{"id": "processing-private-job"}])

    async def fenced_fetch_one(query, values=None):
        create_db.fetch_one_calls.append((query, dict(values or {})))
        if "SET status = 'discarded'" in query:
            return None
        return None

    create_db.fetch_one = fenced_fetch_one
    monkeypatch.setattr(jobs, "db", create_db)

    count = await jobs._discard_recent_private_jobs_superseded_by_comment_callback(
        values={"entity_type": "leads", "entity_id": "100"},
        normalized_message="precioo",
        comment_event_timestamp=1_786_023_182,
    )

    assert count == 0
    discard_query = next(
        query for query, _values in create_db.fetch_one_calls if "SET status = 'discarded'" in query
    )
    assert "NOT EXISTS" in discard_query


@pytest.mark.asyncio
async def test_same_text_dm_and_comment_discards_only_best_mirror_candidate(monkeypatch):
    from app.integrations.kommo import jobs

    create_db = _CallbackCreateDB(
        superseded_private_jobs=[
            {
                "id": "mirror-job",
                "timestamp_delta_seconds": 0.2,
                "comment_timestamp_source": "meta_event_timestamp",
            },
            {"id": "genuine-dm-job", "timestamp_delta_seconds": 0.4},
        ]
    )
    monkeypatch.setattr(jobs, "db", create_db)

    count = await jobs._discard_recent_private_jobs_superseded_by_comment_callback(
        values={"entity_type": "leads", "entity_id": "100"},
        normalized_message="precio?",
        comment_event_timestamp=1_786_023_182,
    )

    assert count == 1
    discard_values = next(
        values
        for query, values in create_db.fetch_one_calls
        if "SET status = 'discarded'" in query
    )
    assert discard_values["job_id"] == "mirror-job"


@pytest.mark.asyncio
async def test_two_equally_plausible_private_candidates_are_left_untouched(monkeypatch):
    from app.integrations.kommo import jobs

    create_db = _CallbackCreateDB(
        superseded_private_jobs=[
            {"id": "candidate-a", "timestamp_delta_seconds": 0.2},
            {"id": "candidate-b", "timestamp_delta_seconds": 0.8},
        ]
    )
    monkeypatch.setattr(jobs, "db", create_db)

    count = await jobs._discard_recent_private_jobs_superseded_by_comment_callback(
        values={"entity_type": "leads", "entity_id": "100"},
        normalized_message="precio?",
        comment_event_timestamp=1_786_023_182,
    )

    assert count == 0
    assert not any("SET status = 'discarded'" in query for query, _values in create_db.fetch_one_calls)


def test_salesbot_iat_fallback_with_large_delta_is_rejected():
    from app.integrations.kommo import jobs

    candidate = jobs._unique_best_mirror_candidate(
        [
            {
                "id": "candidate",
                "timestamp_delta_seconds": jobs.COMMENT_MIRROR_FALLBACK_MAX_DELTA_SECONDS + 0.1,
                "private_timestamp_source": "receipt_received_at",
                "comment_timestamp_source": "salesbot_iat",
            }
        ]
    )

    assert candidate is None


def test_salesbot_iat_fallback_with_small_delta_is_allowed():
    from app.integrations.kommo import jobs

    candidate = jobs._unique_best_mirror_candidate(
        [
            {
                "id": "candidate",
                "timestamp_delta_seconds": jobs.COMMENT_MIRROR_FALLBACK_MAX_DELTA_SECONDS,
                "private_timestamp_source": "receipt_received_at",
                "comment_timestamp_source": "salesbot_iat",
            }
        ]
    )

    assert candidate is not None
    assert candidate["id"] == "candidate"


def test_matched_meta_timestamp_can_use_configured_wider_window():
    from app.integrations.kommo import jobs

    candidate = jobs._unique_best_mirror_candidate(
        [
            {
                "id": "candidate",
                "timestamp_delta_seconds": jobs.COMMENT_MIRROR_FALLBACK_MAX_DELTA_SECONDS + 10,
                "private_timestamp_source": "receipt_received_at",
                "comment_timestamp_source": "meta_event_timestamp",
            }
        ]
    )

    assert candidate is not None
    assert candidate["id"] == "candidate"


@pytest.mark.asyncio
async def test_callback_reconciliation_uses_configured_meta_window(monkeypatch):
    from app.integrations.kommo import jobs

    create_db = _CallbackCreateDB()
    monkeypatch.setattr(jobs, "db", create_db)
    monkeypatch.setattr(
        jobs,
        "get_config",
        lambda: SimpleNamespace(meta_context_match_window_seconds=27),
    )

    await jobs._discard_recent_private_jobs_superseded_by_comment_callback(
        values={"entity_type": "leads", "entity_id": "100"},
        normalized_message="precio?",
        comment_event_timestamp=1_786_023_182,
    )

    assert create_db.fetch_all_calls[0][1]["window_seconds"] == 27


@pytest.mark.asyncio
async def test_repeated_comment_callback_is_idempotent(monkeypatch):
    from app.integrations.kommo import jobs

    first_values = jobs._callback_values(
        SalesbotWidgetData(message="Precio?", lead_id="100", origin="instagram", interaction_type="instagram_comment"),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        {"jti": "token-id", "iat": 123456, "account_id": 123, "entity_type": "leads", "entity_id": "100"},
    )
    external_message_id, _correlation_id = jobs._comment_callback_ids(first_values, "Precio?")
    create_db = _CallbackCreateDB(
        duplicate_job={
            "id": "existing-comment",
            "status": "sent",
            "interaction_type": "instagram_comment",
            "external_message_id": external_message_id,
        }
    )
    monkeypatch.setattr(jobs, "db", create_db)

    result = await jobs.persist_salesbot_callback(
        SalesbotWidgetData(message="Precio?", lead_id="100", origin="instagram", interaction_type="instagram_comment"),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        {"jti": "token-id", "iat": 123456, "account_id": 123, "entity_type": "leads", "entity_id": "100"},
    )

    assert result == {"status": "duplicate", "job_id": "existing-comment"}
    duplicate_values = create_db.fetch_one_calls[1][1]
    assert duplicate_values["salesbot_token_jti"] == "token-id"
    assert duplicate_values["salesbot_token_iat_text"] == "123456"
    assert not any("INSERT INTO kommo_message_jobs" in query for query, _values in create_db.fetch_one_calls)


@pytest.mark.asyncio
async def test_later_comment_from_same_lead_is_not_duplicate_when_callback_identity_changes(monkeypatch):
    from app.integrations.kommo import jobs

    first_values = jobs._callback_values(
        SalesbotWidgetData(message="Precio?", lead_id="100", origin="instagram", interaction_type="instagram_comment"),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        {"jti": "reused-token-id", "iat": 1000, "account_id": 123, "entity_type": "leads", "entity_id": "100"},
    )
    later_values = jobs._callback_values(
        SalesbotWidgetData(message="Precio?", lead_id="100", origin="instagram", interaction_type="instagram_comment"),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/99",
        {"jti": "reused-token-id", "iat": 2000, "account_id": 123, "entity_type": "leads", "entity_id": "100"},
    )
    first_external_id, _ = jobs._comment_callback_ids(first_values, "Precio?")
    later_external_id, _ = jobs._comment_callback_ids(later_values, "Precio?")
    create_db = _CallbackCreateDB(
        duplicate_job={
            "id": "old-sent-comment",
            "status": "sent",
            "interaction_type": "instagram_comment",
            "external_message_id": first_external_id,
        },
        duplicate_is_stale=True,
    )
    monkeypatch.setattr(jobs, "db", create_db)

    result = await jobs.persist_salesbot_callback(
        SalesbotWidgetData(message="Precio?", lead_id="100", origin="instagram", interaction_type="instagram_comment"),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/99",
        {"jti": "reused-token-id", "iat": 2000, "account_id": 123, "entity_type": "leads", "entity_id": "100"},
    )

    assert first_external_id != later_external_id
    assert result == {"status": "ready", "job_id": "comment-job"}
    insert_values = next(values for query, values in create_db.fetch_one_calls if "INSERT INTO kommo_message_jobs" in query)
    assert insert_values["external_message_id"] == later_external_id


@pytest.mark.asyncio
async def test_comment_callback_requires_signed_entity_identity(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    monkeypatch.setattr(jobs, "db", mock_db)

    with pytest.raises(ValueError, match="missing_signed_entity_identity"):
        await jobs.persist_salesbot_callback(
            SalesbotWidgetData(message="Precio?", interaction_type="instagram_comment"),
            "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
            {},
        )

    mock_db.fetch_one.assert_not_called()


@pytest.mark.asyncio
async def test_comment_callback_context_cannot_be_replayed_as_private_message(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    monkeypatch.setattr(jobs, "db", mock_db)

    with pytest.raises(ValueError, match="comment_callback_interaction_type_mismatch"):
        await jobs.persist_salesbot_callback(
            SalesbotWidgetData(
                message="Precio?",
                lead_id="100",
                interaction_type="private_message",
                comment_id="comment-1",
                post_id="post-1",
            ),
            "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
            {"jti": "token-id", "iat": 123456, "account_id": 123, "entity_type": "leads", "entity_id": "100"},
        )

    mock_db.fetch_one.assert_not_called()


@pytest.mark.asyncio
async def test_valid_lead_callback_uses_exact_update_bind_parameters(monkeypatch):
    from app.integrations.kommo import jobs

    strict_db = _StrictCallbackDB(update_job={"id": "lead-job", "status": "ready"})
    monkeypatch.setattr(jobs, "db", strict_db)

    result = await jobs.persist_salesbot_callback(
        SalesbotWidgetData(lead_id="100", contact_id="200"),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        {
            "jti": "token-id",
            "iat": 123456,
            "account_id": 123,
            "user_id": 456,
            "client_uid": "client-uuid",
            "entity_type": "leads",
            "entity_id": "100",
        },
    )

    assert result == {"status": "ready", "job_id": "lead-job"}
    query, values = strict_db.calls[0]
    assert "candidate.lead_id = :entity_id" in query
    assert values == {
        "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        "entity_id": "100",
        "entity_type": "leads",
        "widget_contact_id": "200",
        "callback_claims": values["callback_claims"],
        "salesbot_token_jti": "token-id",
        "salesbot_token_iat": 123456.0,
        "salesbot_token_iat_text": "123456",
        "salesbot_account_id": "123",
        "salesbot_user_id": "456",
        "salesbot_client_uuid": "client-uuid",
        "interaction_type": "private_message",
        "expected_channel": None,
        "author_username": None,
        "author_profile_url": None,
        "sender_username": None,
        "sender_profile_url": None,
    }


@pytest.mark.asyncio
async def test_valid_contact_callback_uses_exact_update_bind_parameters(monkeypatch):
    from app.integrations.kommo import jobs

    strict_db = _StrictCallbackDB(update_job={"id": "contact-job", "status": "ready"})
    monkeypatch.setattr(jobs, "db", strict_db)

    result = await jobs.persist_salesbot_callback(
        SalesbotWidgetData(contact_id="200"),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        {
            "iat": 123456,
            "account_id": 123,
            "client_uid": "client-uuid",
            "entity_type": "contacts",
            "entity_id": "200",
        },
    )

    assert result == {"status": "ready", "job_id": "contact-job"}
    query, values = strict_db.calls[0]
    assert "candidate.contact_id = :entity_id" in query
    assert set(values) == {
        "return_url",
        "entity_id",
        "entity_type",
        "widget_contact_id",
        "callback_claims",
        "salesbot_token_jti",
        "salesbot_token_iat",
        "salesbot_token_iat_text",
        "salesbot_account_id",
        "salesbot_user_id",
        "salesbot_client_uuid",
        "interaction_type",
        "expected_channel",
        "author_username",
        "author_profile_url",
        "sender_username",
        "sender_profile_url",
    }
    assert values["entity_type"] == "contacts"
    assert values["entity_id"] == "200"
    assert values["widget_contact_id"] == "200"
    assert values["interaction_type"] == "private_message"
    assert values["expected_channel"] is None


@pytest.mark.asyncio
async def test_ready_instagram_dm_waits_for_delivery_without_persisting_history(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db, client, _generate, deliver, store_message = _install_direct_ready_job(
        monkeypatch,
        jobs,
    )
    job = {
        "id": "job",
        "processing_lease_id": LEASE_ID,
        "return_url": None,
        "combined_message": "Precio?",
        "channel": "instagram",
        "interaction_type": "private_message",
        "talk_id": "300",
        "correlation_id": "corr",
    }

    await jobs._process_ready_job(job)

    deliver.assert_awaited_once()
    assert deliver.await_args.kwargs["job"]["talk_id"] == "300"
    assert deliver.await_args.kwargs["customer_text"] == "Respuesta directa"
    client.run_salesbot.assert_not_awaited()
    client.continue_salesbot.assert_not_awaited()
    store_message.assert_not_awaited()
    waiting_call = next(
        call
        for call in mock_db.fetch_one.await_args_list
        if "SET status = 'waiting_for_delivery'" in call.args[0]
    )
    assert "status = 'processing'" in waiting_call.args[0]
    assert json.loads(waiting_call.args[1]["delivery_response"]) == {
        "transport": "chats_api",
        "provider_message_ids": ["instagram-message"],
    }
    pending = json.loads(waiting_call.args[1]["pending_assistant_message"])
    assert pending["content"] == "Respuesta directa"
    assert pending["customer_id"] == "customer"
    assert pending["final_status"] == "sent"


@pytest.mark.asyncio
@pytest.mark.parametrize("include_pdf", [False, True])
async def test_ready_instagram_product_image_persists_only_semantic_image(
    monkeypatch,
    include_pdf,
):
    from app.integrations.kommo import jobs
    from app.integrations.kommo.delivery import DeliveryResult

    image_url = "https://cdn.example/private-product.jpg"
    ai_result = {
        "text": f"Aqui tienes la foto. {image_url}",
        "product_image": {
            "type": "product_image",
            "caption": "Aqui tienes la foto.",
            "image_url": image_url,
            "product_name": "Pijama Satin",
            "sku": "PJ-1",
        },
        "escalated": False,
    }
    if include_pdf:
        ai_result["catalog_pdf"] = {
            "type": "catalog_pdf",
            "caption": "El catalogo se envia por WhatsApp.",
        }
    semantic_image = {
        "type": "product_image",
        "product_name": "Pijama Satin",
        "sku": "PJ-1",
    }
    delivery_result = DeliveryResult(
        transport="chats_api",
        customer_text="Aqui tienes la foto.",
        delivered_attachments=[semantic_image],
        provider_message_ids=["instagram-image"],
    )
    mock_db, client, _generate, deliver, store_message = _install_direct_ready_job(
        monkeypatch,
        jobs,
        ai_result=ai_result,
        delivery_result=delivery_result,
    )
    monkeypatch.setattr(
        jobs,
        "get_config",
        lambda: SimpleNamespace(
            kommo_ai_active_enum_id=1,
            instagram_story_context_ttl_hours=24,
            kommo_chats_media_enabled=True,
            kommo_chats_product_images_enabled=True,
            kommo_chats_catalog_pdf_enabled=True,
        ),
    )

    await jobs._process_ready_job(
        {
            "id": "job",
            "processing_lease_id": LEASE_ID,
            "return_url": None,
            "combined_message": "Enviame foto y catalogo",
            "channel": "instagram",
            "interaction_type": "private_message",
            "talk_id": "300",
            "correlation_id": "corr",
        }
    )

    assert deliver.await_args.kwargs["customer_text"] == "Aqui tienes la foto."
    assert image_url not in deliver.await_args.kwargs["customer_text"]
    store_message.assert_not_awaited()
    waiting_call = next(
        call for call in mock_db.fetch_one.await_args_list
        if "SET status = 'waiting_for_delivery'" in call.args[0]
    )
    pending = json.loads(waiting_call.args[1]["pending_assistant_message"])
    assert pending["attachments"] == [semantic_image]
    assert all(item["type"] != "catalog_pdf" for item in pending["attachments"])
    client.continue_salesbot.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_instagram_catalog_handoff_persists_text_without_pdf(monkeypatch):
    from app.integrations.kommo import jobs
    from app.integrations.kommo.delivery import DeliveryResult

    handoff = "Te envio el catalogo por WhatsApp."
    result = DeliveryResult(
        transport="chats_api",
        customer_text=handoff,
        delivered_attachments=[],
        provider_message_ids=["instagram-text"],
    )
    mock_db, client, _generate, deliver, store_message = _install_direct_ready_job(
        monkeypatch,
        jobs,
        ai_result={
            "text": handoff,
            "catalog_pdf": {"type": "catalog_pdf", "caption": "Catalogo"},
            "escalated": False,
        },
        delivery_result=result,
    )

    await jobs._process_ready_job(
        {
            "id": "job",
            "processing_lease_id": LEASE_ID,
            "return_url": None,
            "combined_message": "Catalogo",
            "channel": "instagram",
            "interaction_type": "private_message",
            "talk_id": "300",
            "correlation_id": "corr",
        }
    )

    assert deliver.await_args.kwargs["customer_text"] == handoff
    store_message.assert_not_awaited()
    waiting_call = next(
        call for call in mock_db.fetch_one.await_args_list
        if "SET status = 'waiting_for_delivery'" in call.args[0]
    )
    assert json.loads(waiting_call.args[1]["pending_assistant_message"])["attachments"] == []
    client.continue_salesbot.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delivery_error", "expected_status"),
    [
        (KommoAPIError("quota exhausted", status_code=402), "failed"),
        (KommoAPIError("missing permission", status_code=403), "failed"),
        (KommoAPIError("missing talk", status_code=404), "failed"),
        (KommoAPIError("closed talk", status_code=422), "failed"),
        (KommoDeliveryUnknownError("connection lost"), "delivery_unknown"),
    ],
)
async def test_ready_instagram_dm_delivery_failure_is_safely_terminal(
    monkeypatch,
    delivery_error,
    expected_status,
):
    from app.integrations.kommo import jobs

    mock_db, client, _generate, deliver, store_message = _install_direct_ready_job(
        monkeypatch,
        jobs,
        delivery_error=delivery_error,
        customer_send_begun=True,
    )

    await jobs._process_ready_job(
        {
            "id": "job",
            "processing_lease_id": LEASE_ID,
            "return_url": None,
            "combined_message": "Precio?",
            "channel": "instagram",
            "interaction_type": "private_message",
            "talk_id": "300",
            "correlation_id": "corr",
        }
    )

    deliver.assert_awaited_once()
    if isinstance(delivery_error, KommoDeliveryUnknownError):
        jobs._direct_customer_send_begun.assert_not_awaited()
    else:
        jobs._direct_customer_send_begun.assert_awaited_once_with("job")
    client.run_salesbot.assert_not_awaited()
    client.continue_salesbot.assert_not_awaited()
    client.send_talk_message.assert_not_called()
    store_message.assert_not_awaited()
    assert mock_db.execute.await_args.args[1]["status"] == expected_status


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["download", "drive_upload"])
async def test_instagram_image_preflight_failure_sends_one_safe_direct_fallback(
    monkeypatch,
    failure_stage,
):
    from app.integrations.kommo import delivery, jobs
    from app.integrations.kommo.files import KommoDownloadedImage

    ai_result = {
        "text": "Aqui tienes la foto",
        "product_image": {
            "type": "product_image",
            "image_url": "https://cdn.example/product.jpg",
            "product_name": "Pijama",
            "sku": "PJ-1",
        },
        "escalated": False,
    }
    mock_db, client, _generate, _mock_deliver, store_message = _install_direct_ready_job(
        monkeypatch,
        jobs,
        ai_result=ai_result,
        customer_send_begun=False,
    )
    config = SimpleNamespace(
        kommo_ai_active_enum_id=1,
        instagram_story_context_ttl_hours=24,
        kommo_chats_media_enabled=True,
        kommo_chats_product_images_enabled=True,
        kommo_chats_catalog_pdf_enabled=False,
        kommo_chats_pdf_attachment_type=None,
    )
    monkeypatch.setattr(jobs, "get_config", lambda: config)
    monkeypatch.setattr(delivery, "get_config", lambda: config)
    monkeypatch.setattr(jobs, "deliver_response", delivery.deliver_response)
    downloaded = KommoDownloadedImage(
        data=b"image-bytes",
        file_name="product.jpg",
        mime_type="image/jpeg",
        content_hash="a" * 64,
    )
    files = SimpleNamespace(
        client=client,
        download_image=AsyncMock(
            side_effect=(
                KommoAPIError("image download failed")
                if failure_stage == "download"
                else None
            ),
            return_value=downloaded,
        ),
        upload_downloaded_image=AsyncMock(
            side_effect=KommoAPIError("Drive upload failed")
        ),
    )
    monkeypatch.setattr(delivery.KommoFiles, "from_config", lambda _client: files)
    monkeypatch.setattr(delivery, "_find_cached_upload", AsyncMock(return_value=None))
    claim = AsyncMock(return_value=delivery._DeliveryClaim("sending", None, True))
    monkeypatch.setattr(delivery, "_claim_delivery", claim)
    client.send_talk_message = AsyncMock(return_value={"id": "fallback-message"})
    monkeypatch.setattr(
        delivery.db,
        "fetch_one",
        AsyncMock(return_value={"provider_message_id": "fallback-message"}),
    )

    await jobs._process_ready_job(
        {
            "id": "job",
            "processing_lease_id": LEASE_ID,
            "return_url": None,
            "combined_message": "Enviame la foto",
            "channel": "instagram",
            "interaction_type": "private_message",
            "talk_id": "300",
            "correlation_id": "corr",
        }
    )

    assert claim.await_count == 1
    assert claim.await_args.kwargs["media_type"] == "text"
    assert claim.await_args.kwargs["attachment_metadata"]["delivery_purpose"] == "fallback"
    client.send_talk_message.assert_awaited_once_with(
        "300",
        text=jobs.CUSTOMER_DELIVERY_FAILURE_MESSAGE,
    )
    client.continue_salesbot.assert_not_awaited()
    store_message.assert_not_awaited()
    waiting_call = next(
        call for call in mock_db.fetch_one.await_args_list
        if "SET status = 'waiting_for_delivery'" in call.args[0]
    )
    pending = json.loads(waiting_call.args[1]["pending_assistant_message"])
    assert pending["content"] == jobs.CUSTOMER_DELIVERY_FAILURE_MESSAGE
    assert pending["final_status"] == "discarded"
    if failure_stage == "download":
        files.upload_downloaded_image.assert_not_awaited()
    else:
        files.upload_downloaded_image.assert_awaited_once_with(downloaded)


@pytest.mark.asyncio
async def test_direct_customer_send_begun_uses_durable_attempt_state(monkeypatch):
    from app.integrations.kommo import jobs

    fetch_one = AsyncMock(return_value={"send_begun": True})
    monkeypatch.setattr(jobs.db, "fetch_one", fetch_one)

    assert await jobs._direct_customer_send_begun("00000000-0000-0000-0000-000000000001") is True

    query, values = fetch_one.await_args.args
    assert "status IN ('sending', 'accepted', 'confirmed', 'delivery_unknown')" in query
    assert "attachment_metadata->>'send_attempt_count'" in query
    assert "::integer >= 1" in query
    assert values == {"job_id": "00000000-0000-0000-0000-000000000001"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outbound_state",
    ["sending", "accepted", "confirmed", "delivery_unknown"],
)
async def test_durable_outbound_state_prevents_direct_failure_fallback(
    monkeypatch,
    outbound_state,
):
    from app.integrations.kommo import jobs

    mock_db, client, _generate, deliver, store_message = _install_direct_ready_job(
        monkeypatch,
        jobs,
        delivery_error=RuntimeError(f"delivery stopped in {outbound_state}"),
        customer_send_begun=True,
    )

    await jobs._process_ready_job(
        {
            "id": "job",
            "processing_lease_id": LEASE_ID,
            "return_url": None,
            "combined_message": "Precio?",
            "channel": "instagram",
            "interaction_type": "private_message",
            "talk_id": "300",
            "correlation_id": "corr",
        }
    )

    deliver.assert_awaited_once()
    jobs._direct_customer_send_begun.assert_awaited_once_with("job")
    client.send_talk_message.assert_not_called()
    client.continue_salesbot.assert_not_awaited()
    store_message.assert_not_awaited()
    assert mock_db.execute.await_args.args[1]["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job_overrides", "ai_result", "expected_reason", "expects_fallback"),
    [
        ({"message_type": "file"}, None, "unsupported_attachment", True),
        ({}, {"text": "", "escalated": False}, "empty_response", False),
    ],
)
async def test_direct_instagram_no_reply_paths_never_continue_salesbot(
    monkeypatch,
    job_overrides,
    ai_result,
    expected_reason,
    expects_fallback,
):
    from app.integrations.kommo import jobs

    mock_db, client, generate, deliver, _store_message = _install_direct_ready_job(
        monkeypatch,
        jobs,
        ai_result=ai_result,
    )
    await jobs._process_ready_job(
        {
            "id": "job",
            "processing_lease_id": LEASE_ID,
            "return_url": None,
            "combined_message": "Contenido",
            "channel": "instagram",
            "interaction_type": "private_message",
            "talk_id": "300",
            "correlation_id": "corr",
            **job_overrides,
        }
    )

    if job_overrides:
        generate.assert_not_awaited()
    client.continue_salesbot.assert_not_awaited()
    if expects_fallback:
        deliver.assert_awaited_once()
        assert deliver.await_args.kwargs["customer_text"] == jobs.UNSUPPORTED_ATTACHMENT_MESSAGE
        assert deliver.await_args.kwargs["job"]["direct_delivery_purpose"] == "fallback"
        assert any("SET status = 'waiting_for_delivery'" in call.args[0] for call in mock_db.fetch_one.await_args_list)
    else:
        deliver.assert_not_awaited()
        terminal_values = mock_db.execute.await_args.args[1]
        assert terminal_values["status"] == "discarded"
        assert terminal_values["last_error"] == expected_reason


@pytest.mark.asyncio
async def test_direct_instagram_ai_failure_sends_safe_fallback_without_salesbot(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db, client, generate, deliver, store_message = _install_direct_ready_job(
        monkeypatch,
        jobs,
    )
    generate.side_effect = RuntimeError("AI unavailable")

    await jobs._process_ready_job(
        {
            "id": "job",
            "processing_lease_id": LEASE_ID,
            "return_url": None,
            "combined_message": "Precio?",
            "channel": "instagram",
            "interaction_type": "private_message",
            "talk_id": "300",
            "correlation_id": "corr",
        }
    )

    deliver.assert_awaited_once()
    assert deliver.await_args.kwargs["customer_text"] == jobs.CUSTOMER_DELIVERY_FAILURE_MESSAGE
    assert deliver.await_args.kwargs["job"]["direct_delivery_purpose"] == "fallback"
    store_message.assert_not_awaited()
    client.continue_salesbot.assert_not_awaited()
    assert any("SET status = 'waiting_for_delivery'" in call.args[0] for call in mock_db.fetch_one.await_args_list)


@pytest.mark.asyncio
async def test_direct_instagram_lost_lease_aborts_without_customer_send(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db, client, _generate, deliver, store_message = _install_direct_ready_job(
        monkeypatch,
        jobs,
        delivery_error=KommoDeliveryAbortedError("processing lease was lost"),
    )

    await jobs._process_ready_job(
        {
            "id": "job",
            "processing_lease_id": LEASE_ID,
            "return_url": None,
            "combined_message": "Precio?",
            "channel": "instagram",
            "interaction_type": "private_message",
            "talk_id": "300",
            "correlation_id": "corr",
        }
    )

    deliver.assert_awaited_once()
    store_message.assert_not_awaited()
    client.continue_salesbot.assert_not_awaited()
    assert mock_db.execute.await_args.args[1]["status"] == "failed"


@pytest.mark.asyncio
async def test_direct_instagram_explicit_fallback_waits_before_persisting_message(monkeypatch):
    from app.integrations.kommo import jobs
    from app.integrations.kommo.delivery import DeliveryResult

    mock_db = _install_ready_job_db(monkeypatch, jobs)
    deliver = AsyncMock(
        return_value=DeliveryResult(
            transport="chats_api",
            customer_text=jobs.UNSUPPORTED_ATTACHMENT_MESSAGE,
            delivered_attachments=[],
            provider_message_ids=["fallback-message"],
        )
    )
    monkeypatch.setattr(jobs, "deliver_response", deliver)
    store_message = AsyncMock()
    monkeypatch.setattr(jobs.conversations, "store_message", store_message)
    client = MagicMock()
    job = {
        "id": "job",
        "processing_lease_id": LEASE_ID,
        "channel": "instagram",
        "interaction_type": "private_message",
        "talk_id": "300",
    }
    customer = {"id": "customer", "channel": "instagram"}

    await jobs._continue_and_discard_job(
        client,
        job,
        "unsupported_attachment",
        customer_message=jobs.UNSUPPORTED_ATTACHMENT_MESSAGE,
        customer=customer,
    )

    deliver.assert_awaited_once()
    store_message.assert_not_awaited()
    waiting_call = next(
        call for call in mock_db.fetch_one.await_args_list
        if "SET status = 'waiting_for_delivery'" in call.args[0]
    )
    pending = json.loads(waiting_call.args[1]["pending_assistant_message"])
    assert pending["content"] == jobs.UNSUPPORTED_ATTACHMENT_MESSAGE
    assert pending["final_status"] == "discarded"


@pytest.mark.asyncio
async def test_waiting_instagram_delivery_delivered_confirms_and_finalizes(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_all = AsyncMock(
        return_value=[
            {
                "id": "outbound",
                "media_type": "text",
                "status": "accepted",
                "provider_message_id": "message-1",
            }
        ]
    )
    monkeypatch.setattr(jobs, "db", mock_db)
    client = SimpleNamespace(
        get_talk_message_delivery_status=AsyncMock(return_value="delivered")
    )
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)
    confirm = AsyncMock()
    finalize = AsyncMock()
    fail = AsyncMock()
    monkeypatch.setattr(jobs, "_confirm_waiting_delivery", confirm)
    monkeypatch.setattr(jobs, "_finalize_waiting_delivery", finalize)
    monkeypatch.setattr(jobs, "_fail_waiting_delivery", fail)

    await jobs._reconcile_waiting_for_delivery_job(
        {
            "id": "job",
            "talk_id": "300",
            "channel": "instagram",
            "continuation_response": {"provider_message_ids": ["message-1"]},
        }
    )

    client.get_talk_message_delivery_status.assert_awaited_once_with("300", "message-1")
    confirm.assert_awaited_once_with("outbound", "message-1")
    finalize.assert_awaited_once_with("job")
    fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_waiting_instagram_delivery_error_fails_without_finalizing(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_all = AsyncMock(
        return_value=[
            {
                "id": "outbound",
                "media_type": "product_image",
                "status": "accepted",
                "provider_message_id": "message-1",
            }
        ]
    )
    monkeypatch.setattr(jobs, "db", mock_db)
    client = SimpleNamespace(get_talk_message_delivery_status=AsyncMock(return_value="error"))
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)
    confirm = AsyncMock()
    finalize = AsyncMock()
    fail = AsyncMock()
    monkeypatch.setattr(jobs, "_confirm_waiting_delivery", confirm)
    monkeypatch.setattr(jobs, "_finalize_waiting_delivery", finalize)
    monkeypatch.setattr(jobs, "_fail_waiting_delivery", fail)
    job = {
        "id": "job",
        "talk_id": "300",
        "channel": "instagram",
        "continuation_response": {"provider_message_ids": ["message-1"]},
    }

    await jobs._reconcile_waiting_for_delivery_job(job)

    fail.assert_awaited_once_with(
        job,
        "message-1",
        "error",
        media_type="product_image",
    )
    confirm.assert_not_awaited()
    finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivery_error_marks_outbound_and_job_failed_with_safe_diagnostic(
    monkeypatch,
    caplog,
):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)
    job = {"id": "job", "talk_id": "300"}

    await jobs._fail_waiting_delivery(job, "message-1", "error", media_type="text")

    assert mock_db.execute.await_count == 2
    outbound_call, job_call = mock_db.execute.await_args_list
    assert "SET status = 'failed'" in outbound_call.args[0]
    assert "provider_message_id = :provider_message_id" in outbound_call.args[0]
    assert "SET status = 'failed'" in job_call.args[0]
    assert "status = 'waiting_for_delivery'" in job_call.args[0]
    assert outbound_call.args[1]["last_error"] == job_call.args[1]["last_error"]
    assert "delivery_status=error" in outbound_call.args[1]["last_error"]
    assert "job_id=job" in caplog.text
    assert "talk_id=300" in caplog.text
    assert "provider_message_id=message-1" in caplog.text
    assert "provider_delivery_status=error" in caplog.text


@pytest.mark.asyncio
async def test_media_delivery_error_queues_original_text_without_attachment(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    mock_db.get_settings = AsyncMock(return_value={"store_phone_number": "+58 412 1234567"})
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)
    job = {
        "id": "job",
        "talk_id": "300",
        "pending_assistant_message": {
            "customer_id": "customer",
            "content": "La bruma cuesta $24.",
            "attachments": [{"type": "product_image", "product_name": "Bruma"}],
            "final_status": "sent",
        },
    }

    await jobs._fail_waiting_delivery(
        job,
        "message-1",
        "error",
        media_type="product_image",
    )

    assert mock_db.execute.await_count == 2
    job_call = mock_db.execute.await_args_list[1]
    assert "SET status = 'ready'" in job_call.args[0]
    pending = json.loads(job_call.args[1]["pending_assistant_message"])
    assert pending["content"].startswith("Parece que hay un error aquí en Instagram")
    assert "Intenta escribirnos por WhatsApp aquí y seguro te ayudamos:" in pending["content"]
    assert "https://wa.me/584121234567" in pending["content"]
    assert "?text=" in pending["content"]
    assert pending["whatsapp_handoff"]["prefilled_message"] == "Hola, quiero la foto de Bruma."
    assert pending["attachments"] == []
    assert pending["delivery_failure_fallback"] is True


@pytest.mark.asyncio
async def test_provider_error_text_fallback_delivers_without_running_ai(monkeypatch):
    from app.integrations.kommo import jobs
    from app.integrations.kommo.delivery import DeliveryResult

    deliver = AsyncMock(
        return_value=DeliveryResult(
            transport="chats_api",
            customer_text="La bruma cuesta $24.",
            delivered_attachments=[],
            provider_message_ids=["fallback-message"],
        )
    )
    mark_waiting = AsyncMock(return_value=True)
    monkeypatch.setattr(jobs, "deliver_response", deliver)
    monkeypatch.setattr(jobs, "_mark_direct_job_waiting_for_delivery", mark_waiting)
    client = MagicMock()
    job = {
        "id": "job",
        "processing_lease_id": LEASE_ID,
        "channel": "instagram",
        "interaction_type": "private_message",
        "talk_id": "300",
        "last_error": "provider error",
    }
    pending = {
        "customer_id": "customer",
        "content": "La bruma cuesta $24.",
        "attachments": [],
        "final_status": "sent",
        "delivery_failure_fallback": True,
    }

    await jobs._process_provider_error_text_fallback(client, job, pending)

    deliver.assert_awaited_once()
    assert deliver.await_args.kwargs["result"] == {}
    assert deliver.await_args.kwargs["customer_text"] == "La bruma cuesta $24."
    assert (
        deliver.await_args.kwargs["job"]["direct_delivery_purpose"]
        == "provider_error_fallback"
    )
    mark_waiting.assert_awaited_once()
    waiting_payload = mark_waiting.await_args.args[4]
    assert waiting_payload["attachments"] == []
    assert waiting_payload["delivery_failure_fallback"] is True


@pytest.mark.asyncio
async def test_provider_error_text_fallback_is_unknown_if_accepted_state_cannot_persist(
    monkeypatch,
):
    from app.integrations.kommo import jobs
    from app.integrations.kommo.delivery import DeliveryResult

    monkeypatch.setattr(
        jobs,
        "deliver_response",
        AsyncMock(
            return_value=DeliveryResult(
                transport="chats_api",
                customer_text="La bruma cuesta $24.",
                delivered_attachments=[],
                provider_message_ids=["fallback-message"],
            )
        ),
    )
    monkeypatch.setattr(
        jobs,
        "_mark_direct_job_waiting_for_delivery",
        AsyncMock(return_value=False),
    )
    mark_job = AsyncMock()
    monkeypatch.setattr(jobs, "_mark_job", mark_job)
    job = {
        "id": "job",
        "processing_lease_id": LEASE_ID,
        "channel": "instagram",
        "interaction_type": "private_message",
        "talk_id": "300",
    }

    await jobs._process_provider_error_text_fallback(
        MagicMock(),
        job,
        {
            "content": "La bruma cuesta $24.",
            "delivery_failure_fallback": True,
        },
    )

    assert mark_job.await_args.args[1] == "delivery_unknown"


@pytest.mark.asyncio
async def test_waiting_instagram_delivery_sent_remains_pending_without_resend(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_all = AsyncMock(
        return_value=[
            {
                "id": "outbound",
                "media_type": "text",
                "status": "accepted",
                "provider_message_id": "message-1",
            }
        ]
    )
    monkeypatch.setattr(jobs, "db", mock_db)
    client = SimpleNamespace(get_talk_message_delivery_status=AsyncMock(return_value="sent"))
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)
    confirm = AsyncMock()
    finalize = AsyncMock()
    fail = AsyncMock()
    monkeypatch.setattr(jobs, "_confirm_waiting_delivery", confirm)
    monkeypatch.setattr(jobs, "_finalize_waiting_delivery", finalize)
    monkeypatch.setattr(jobs, "_fail_waiting_delivery", fail)

    await jobs._reconcile_waiting_for_delivery_job(
        {
            "id": "job",
            "talk_id": "300",
            "continuation_response": {"provider_message_ids": ["message-1"]},
        }
    )

    confirm.assert_not_awaited()
    finalize.assert_not_awaited()
    fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_delivery_finalization_persists_assistant_once(monkeypatch):
    from app.integrations.kommo import jobs

    pending = {
        "customer_id": "customer",
        "content": "Respuesta",
        "attachments": [],
        "final_status": "sent",
    }
    job = {
        "id": "job",
        "talk_id": "300",
        "status": "waiting_for_delivery",
        "pending_assistant_message": pending,
        "continuation_response": {"provider_message_ids": ["message-1"]},
    }
    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(
        side_effect=[job, {"id": "job", "talk_id": "300"}, None]
    )
    monkeypatch.setattr(jobs, "db", mock_db)
    persist = AsyncMock()
    monkeypatch.setattr(jobs, "_persist_waiting_assistant_message", persist)

    await jobs._finalize_waiting_delivery("job")
    await jobs._finalize_waiting_delivery("job")

    persist.assert_awaited_once_with(job, pending)


@pytest.mark.asyncio
async def test_duplicate_assistant_persistence_is_exactly_once(monkeypatch):
    from app.integrations.kommo import jobs

    state = {"persisted": False}
    mock_db = MagicMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())

    async def fetch_one(_query, _values=None):
        return {
            "assistant_message_persisted_at": "already"
            if state["persisted"]
            else None
        }

    async def execute(_query, _values=None):
        state["persisted"] = True

    mock_db.fetch_one = AsyncMock(side_effect=fetch_one)
    mock_db.execute = AsyncMock(side_effect=execute)
    monkeypatch.setattr(jobs, "db", mock_db)
    store_message = AsyncMock()
    monkeypatch.setattr(jobs.conversations, "store_message", store_message)
    job = {"id": "job", "channel": "instagram", "interaction_type": "private_message"}
    payload = {
        "customer_id": "customer",
        "channel": "instagram",
        "content": "Respuesta",
        "function_calls": None,
        "attachments": [],
    }

    await jobs._persist_waiting_assistant_message(job, payload)
    await jobs._persist_waiting_assistant_message(job, payload)

    store_message.assert_awaited_once_with(
        customer_id="customer",
        role="assistant",
        content="Respuesta",
        channel="instagram",
        function_calls=None,
        source_id="kommo-job:job",
        attachments=None,
        interaction_type="private_message",
    )


@pytest.mark.asyncio
async def test_outgoing_webhook_trigger_does_not_confirm_delivery(monkeypatch):
    from app.integrations.kommo import jobs

    fetch_one = AsyncMock(return_value={"id": "job"})
    monkeypatch.setattr(jobs.db, "fetch_one", fetch_one)

    assert await jobs.request_delivery_reconciliation("message-1") is True

    query, values = fetch_one.await_args.args
    assert "delivery_reconcile_after_at = NOW()" in query
    assert "status = 'waiting_for_delivery'" in query
    assert "status = 'confirmed'" not in query
    assert values == {"provider_message_id": "message-1"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expected_channel", "waiting_channel"),
    [("instagram", "whatsapp"), ("whatsapp", "instagram")],
)
async def test_salesbot_callback_cannot_consume_other_channel_job(
    monkeypatch,
    caplog,
    expected_channel,
    waiting_channel,
):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(
        side_effect=[None, {"id": "waiting-job", "channel": waiting_channel}]
    )
    monkeypatch.setattr(jobs, "db", mock_db)

    with caplog.at_level("WARNING", logger="app.integrations.kommo.jobs"):
        result = await jobs.persist_salesbot_callback(
            SalesbotWidgetData(lead_id="100", expected_channel=expected_channel),
            "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
            {
                "iat": 123456,
                "account_id": 123,
                "client_uid": "client-uuid",
                "entity_type": "leads",
                "entity_id": "100",
            },
        )

    assert result == {"status": "ignored", "reason": "expected_channel_mismatch"}
    claim_query, claim_values = mock_db.fetch_one.await_args_list[0].args
    assert "candidate.channel = CAST(:expected_channel AS text)" in claim_query
    assert claim_values["expected_channel"] == expected_channel
    assert "reason=expected_channel_mismatch" in caplog.text
    assert "https://" not in caplog.text


@pytest.mark.asyncio
async def test_callback_without_waiting_job_uses_exact_fallback_bind_parameters(monkeypatch):
    from app.integrations.kommo import jobs

    strict_db = _StrictCallbackDB(update_job=None, latest_job=None)
    monkeypatch.setattr(jobs, "db", strict_db)

    result = await jobs.persist_salesbot_callback(
        SalesbotWidgetData(lead_id="100"),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        {
            "iat": 123456,
            "account_id": 123,
            "client_uid": "client-uuid",
            "entity_type": "leads",
            "entity_id": "100",
        },
    )

    assert result == {"status": "ignored", "reason": "no_waiting_job"}
    assert len(strict_db.calls) == 2
    assert strict_db.calls[1][1] == {
        "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        "salesbot_token_jti": None,
        "salesbot_token_iat_text": "123456",
    }


@pytest.mark.asyncio
async def test_duplicate_callback_uses_exact_fallback_bind_parameters(monkeypatch):
    from app.integrations.kommo import jobs

    strict_db = _StrictCallbackDB(update_job=None, latest_job={"id": "sent-job", "status": "sent"})
    monkeypatch.setattr(jobs, "db", strict_db)

    result = await jobs.persist_salesbot_callback(
        SalesbotWidgetData(lead_id="100"),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        {
            "iat": 123456,
            "account_id": 123,
            "client_uid": "client-uuid",
            "entity_type": "leads",
            "entity_id": "100",
        },
    )

    assert result == {"status": "duplicate", "job_id": "sent-job"}
    assert strict_db.calls[1][1] == {
        "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        "salesbot_token_jti": None,
        "salesbot_token_iat_text": "123456",
    }


@pytest.mark.asyncio
async def test_salesbot_callback_rejects_mismatched_widget_lead_id(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)

    with pytest.raises(ValueError, match="lead_id_mismatch"):
        await jobs.persist_salesbot_callback(
            SalesbotWidgetData(lead_id="999"),
            "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
            {"iat": 123456, "account_id": 123, "client_uid": "client-uuid", "entity_type": "leads", "entity_id": "100"},
        )
    mock_db.fetch_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_salesbot_launch_race_preserves_ready_callback(monkeypatch):
    from app.integrations.kommo import jobs

    class _RaceDB:
        def __init__(self):
            self.status = "processing"
            self.ready_count = 0
            self.status_when_run_started = None
            self.execute_queries = []

        async def get_settings(self):
            return {"ai_enabled": True}

        async def fetch_one(self, query, values=None):
            if "SET status = 'waiting_for_salesbot'" in query and "RETURNING" in query:
                assert self.status == "processing"
                self.status = "waiting_for_salesbot"
                return {"id": values["id"], "status": self.status}
            if "SELECT status FROM kommo_message_jobs" in query:
                return {"status": self.status}
            return None

        async def execute(self, query, values=None):
            self.execute_queries.append(query)
            if "SET status = 'waiting_for_salesbot'" in query:
                self.status = "waiting_for_salesbot"
            return None

    race_db = _RaceDB()
    monkeypatch.setattr(jobs, "db", race_db)
    monkeypatch.setattr(
        jobs,
        "get_config",
        lambda: SimpleNamespace(
            channel_backend="kommo",
            kommo_ai_active_enum_id=1,
            kommo_whatsapp_salesbot_id=556,
            kommo_salesbot_id=557,
        ),
    )
    monkeypatch.setattr(jobs, "ensure_ai_mode_initialized", AsyncMock(return_value=(1, False)))
    monkeypatch.setattr(jobs, "sync_local_state_from_ai_mode", AsyncMock())
    monkeypatch.setattr(
        jobs,
        "evaluate_automation_state",
        lambda **_kwargs: SimpleNamespace(allowed=True, reason="allowed", needs_ai_mode_initialization=False),
    )
    monkeypatch.setattr(
        jobs,
        "resolve_customer_from_kommo_job",
        AsyncMock(return_value={"id": "customer", "conversation_state": "active"}),
    )

    client = MagicMock()
    client.get_lead = AsyncMock(return_value={"id": 100})

    async def run_salesbot(_entity_id, _entity_type, salesbot_id):
        assert salesbot_id == 556
        race_db.status_when_run_started = race_db.status
        race_db.status = "ready"
        race_db.ready_count += 1

    client.run_salesbot = AsyncMock(side_effect=run_salesbot)
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)

    await jobs._launch_salesbot_for_job(
        {
            "id": "job",
            "status": "processing",
            "lead_id": "100",
            "contact_id": "200",
            "combined_message": "Hola",
            "channel": "whatsapp",
        }
    )

    assert race_db.status_when_run_started == "waiting_for_salesbot"
    assert race_db.status == "ready"
    assert race_db.ready_count == 1
    assert not any("SET status = 'waiting_for_salesbot'" in query for query in race_db.execute_queries)


@pytest.mark.asyncio
async def test_comment_job_does_not_launch_salesbot_from_backend(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[{"id": "job"}, {"id": "job"}])
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: pytest.fail("comment jobs must not launch Salesbot"))

    await jobs._launch_salesbot_for_job(
        {
            "id": "job",
            "status": "processing",
            "lead_id": "100",
            "contact_id": "200",
            "combined_message": "Precio?",
            "channel": "instagram",
            "interaction_type": "instagram_comment",
        }
    )

    mock_db.execute.assert_awaited_once()
    values = mock_db.execute.await_args.args[1]
    assert values["status"] == "failed"
    assert values["last_error"] == "instagram_comment_requires_native_salesbot_callback"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel", "story_context_enabled"),
    [("whatsapp", True), ("instagram", False)],
)
async def test_non_story_human_mode_keeps_early_suppression(
    monkeypatch,
    channel,
    story_context_enabled,
):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.get_settings = AsyncMock(return_value={"ai_enabled": True})
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)
    monkeypatch.setattr(
        jobs,
        "get_config",
        lambda: SimpleNamespace(
            meta_story_context_enabled=story_context_enabled,
            kommo_whatsapp_salesbot_id=702,
            kommo_salesbot_id=700,
        ),
    )
    monkeypatch.setattr(
        jobs,
        "_discard_private_job_if_superseded_by_recent_comment",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(jobs, "ensure_ai_mode_initialized", AsyncMock(return_value=(2, False)))
    monkeypatch.setattr(jobs, "sync_local_state_from_ai_mode", AsyncMock(return_value=None))
    monkeypatch.setattr(jobs, "_reactivate_expired_escalation_if_needed", AsyncMock(return_value=2))
    monkeypatch.setattr(
        jobs,
        "resolve_customer_from_kommo_job",
        AsyncMock(return_value={"id": "customer", "conversation_state": "escalated"}),
    )
    monkeypatch.setattr(
        jobs,
        "evaluate_automation_state",
        lambda **_kwargs: SimpleNamespace(
            allowed=False,
            reason="kommo_ai_mode_human",
            needs_ai_mode_initialization=False,
        ),
    )
    store_message = AsyncMock()
    monkeypatch.setattr(jobs.conversations, "store_message", store_message)

    client = MagicMock()
    client.get_lead = AsyncMock(return_value={"id": 100})
    client.run_salesbot = AsyncMock()
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)

    job = {
        "id": "job",
        "status": "processing",
        "processing_lease_id": LEASE_ID,
        "lead_id": "100",
        "talk_id": "300",
        "combined_message": "Hola",
        "channel": channel,
        "interaction_type": "private_message",
    }
    if channel == "instagram":
        await jobs._prepare_direct_instagram_dm(job)
    else:
        await jobs._launch_salesbot_for_job(job)

    client.run_salesbot.assert_not_awaited()
    store_message.assert_awaited_once()
    assert store_message.await_args.kwargs["source_id"] == "kommo-job:job"
    query, values = mock_db.execute.await_args.args
    assert "suppress_after_context" not in query
    assert values["status"] == "discarded"
    assert values["last_error"] == "kommo_ai_mode_human"


@pytest.mark.asyncio
async def test_comment_callback_before_private_webhook_job_suppresses_launch(monkeypatch):
    from app.integrations.kommo import jobs

    safety_db = _LaunchSafetyDB(comment_match={"id": "comment-job"})
    monkeypatch.setattr(jobs, "db", safety_db)
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: pytest.fail("private Salesbot must not launch"))

    await jobs._prepare_direct_instagram_dm(
        {
            "id": "private-job",
            "status": "processing",
            "lead_id": "100",
            "contact_id": "200",
            "talk_id": "300",
            "processing_lease_id": LEASE_ID,
            "combined_message": "Precio?",
            "channel": "instagram",
            "origin": "instagram_business",
            "interaction_type": "private_message",
            "created_at": "2026-08-06T13:33:04+00:00",
        }
    )

    match_query, match_values = next(
        (query, values)
        for query, values in safety_db.fetch_one_calls
        if "comment_job.interaction_type = 'instagram_comment'" in query
    )
    assert "NOW()" not in match_query
    assert "CAST(:private_created_at AS timestamptz)" in match_query
    assert match_values["private_created_at"] == "2026-08-06T13:33:04+00:00"
    discard_values = next(
        values for query, values in safety_db.fetch_one_calls if "SET status = 'discarded'" in query
    )
    assert discard_values["reason"] == "superseded_by_instagram_comment"


@pytest.mark.asyncio
async def test_delayed_mirrored_comment_still_suppresses_private_salesbot(monkeypatch):
    from app.integrations.kommo import jobs

    safety_db = _LaunchSafetyDB(comment_match={"id": "comment-job"})
    monkeypatch.setattr(jobs, "db", safety_db)
    original_receipt = "2026-08-06T10:00:00+00:00"

    discarded = await jobs._discard_private_job_if_superseded_by_recent_comment(
        {
            "id": "delayed-private-job",
            "status": "processing",
            "lead_id": "100",
            "contact_id": "200",
            "combined_message": "Precioo",
            "channel": "instagram",
            "origin": "instagram_business",
            "interaction_type": "private_message",
            "created_at": original_receipt,
        }
    )

    assert discarded is True
    query, values = next(
        (query, values)
        for query, values in safety_db.fetch_one_calls
        if "comment_job.interaction_type = 'instagram_comment'" in query
    )
    assert "NOW() -" not in query
    assert "comment_job.callback_claims ->> 'iat'" in query
    assert "meta_event.event_timestamp" in query
    assert values["private_created_at"] == original_receipt
    assert values["window_seconds"] == jobs.COMMENT_MIRROR_RECONCILIATION_SECONDS


@pytest.mark.asyncio
async def test_prelaunch_prefers_matched_meta_timestamp_over_delayed_callback(monkeypatch):
    from app.integrations.kommo import jobs

    safety_db = _LaunchSafetyDB(comment_match={"id": "comment-job"})
    monkeypatch.setattr(jobs, "db", safety_db)

    discarded = await jobs._discard_private_job_if_superseded_by_recent_comment(
        {
            "id": "private-job",
            "status": "processing",
            "lead_id": "100",
            "contact_id": "200",
            "combined_message": "Precio?",
            "channel": "instagram",
            "origin": "instagram_business",
            "interaction_type": "private_message",
            "created_at": "2026-08-06T10:00:00+00:00",
        }
    )

    assert discarded is True
    query = next(
        query
        for query, _values in safety_db.fetch_one_calls
        if "comment_job.interaction_type = 'instagram_comment'" in query
    )
    assert "event.matched_kommo_job_id = comment_job.id" in query
    coalesce_start = query.index("COALESCE(")
    meta_position = query.index("matched_meta_event.event_timestamp", coalesce_start)
    jwt_position = query.index("comment_job.callback_claims ->> 'iat'", coalesce_start)
    assert meta_position < jwt_position


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("story_context_enabled", "expected_status"),
    [(False, "ready"), (True, "waiting_for_context")],
)
async def test_instagram_dm_bypasses_salesbot_and_is_prepared(
    monkeypatch,
    story_context_enabled,
    expected_status,
):
    from app.integrations.kommo import jobs

    safety_db = _LaunchSafetyDB(comment_match=None)
    safety_db.get_settings = AsyncMock(return_value={"ai_enabled": True})
    monkeypatch.setattr(jobs, "db", safety_db)
    monkeypatch.setattr(
        jobs,
        "get_config",
        lambda: SimpleNamespace(
            meta_story_context_enabled=story_context_enabled,
            meta_story_context_wait_seconds=3,
            kommo_whatsapp_salesbot_id=702,
            kommo_salesbot_id=700,
        ),
    )
    monkeypatch.setattr(jobs, "ensure_ai_mode_initialized", AsyncMock(return_value=(1, False)))
    monkeypatch.setattr(jobs, "sync_local_state_from_ai_mode", AsyncMock(return_value=None))
    monkeypatch.setattr(jobs, "_reactivate_expired_escalation_if_needed", AsyncMock(return_value=1))
    monkeypatch.setattr(
        jobs,
        "evaluate_automation_state",
        lambda **_kwargs: SimpleNamespace(allowed=True, reason=None, needs_ai_mode_initialization=False),
    )
    monkeypatch.setattr(
        jobs,
        "resolve_customer_from_kommo_job",
        AsyncMock(return_value={"id": "customer", "conversation_state": "active"}),
    )

    client = MagicMock()
    client.get_lead = AsyncMock(return_value={"id": 100})
    client.run_salesbot = AsyncMock()
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)

    await jobs._prepare_direct_instagram_dm(
        {
            "id": "private-job",
            "status": "processing",
            "lead_id": "100",
            "contact_id": "200",
            "talk_id": "300",
            "processing_lease_id": LEASE_ID,
            "combined_message": "Precio?",
            "channel": "instagram",
            "origin": "instagram_business",
            "interaction_type": "private_message",
            "created_at": "2026-08-06T13:33:04+00:00",
        }
    )

    client.run_salesbot.assert_not_awaited()
    prepared_query, prepared_values = next(
        (query, values)
        for query, values in safety_db.fetch_one_calls
        if "SET status = CASE WHEN :wait_for_story_context" in query
    )
    assert "return_url" not in prepared_query
    assert prepared_values["wait_for_story_context"] is story_context_enabled
    assert safety_db.direct_prepared_status == expected_status
    assert not any(call[1].get("last_error") == "superseded_by_instagram_comment" for call in safety_db.execute_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("talk_id", [None, "", "0", "-1", "abc", True])
async def test_instagram_dm_with_invalid_talk_id_fails_without_transport(monkeypatch, talk_id):
    from app.integrations.kommo import jobs

    mark_job = AsyncMock()
    from_config = MagicMock()
    monkeypatch.setattr(jobs, "_mark_job", mark_job)
    monkeypatch.setattr(jobs.KommoClient, "from_config", from_config)

    await jobs._prepare_direct_instagram_dm(
        {
            "id": "private-job",
            "status": "processing",
            "processing_lease_id": LEASE_ID,
            "talk_id": talk_id,
            "channel": "instagram",
            "interaction_type": "private_message",
        }
    )

    mark_job.assert_awaited_once_with(
        "private-job",
        "failed",
        "missing_instagram_talk_id",
        processing_lease_id=LEASE_ID,
    )
    from_config.assert_not_called()


@pytest.mark.asyncio
async def test_pending_processor_routes_only_instagram_private_messages_directly(monkeypatch):
    from app.integrations.kommo import jobs

    claimed_jobs = [
        {"id": "instagram-dm", "channel": "instagram", "interaction_type": "private_message"},
        {"id": "whatsapp-dm", "channel": "whatsapp", "interaction_type": "private_message"},
        None,
    ]
    monkeypatch.setattr(jobs, "get_config", lambda: SimpleNamespace(channel_backend="kommo"))
    monkeypatch.setattr(jobs, "_claim_due_pending_job", AsyncMock(side_effect=claimed_jobs))
    direct = AsyncMock()
    salesbot = AsyncMock()
    monkeypatch.setattr(jobs, "_prepare_direct_instagram_dm", direct)
    monkeypatch.setattr(jobs, "_launch_salesbot_for_job", salesbot)

    assert await jobs.process_pending_jobs(limit=3) == 2
    direct.assert_awaited_once_with(claimed_jobs[0])
    salesbot.assert_awaited_once_with(claimed_jobs[1])


@pytest.mark.parametrize(
    ("whatsapp_id", "fallback_id", "expected"),
    [
        (702, 700, 702),
        (None, 700, 700),
    ],
)
def test_private_message_salesbot_id_routing(
    monkeypatch,
    whatsapp_id,
    fallback_id,
    expected,
):
    from app.integrations.kommo import jobs

    monkeypatch.setattr(
        jobs,
        "get_config",
        lambda: SimpleNamespace(
            kommo_whatsapp_salesbot_id=whatsapp_id,
            kommo_salesbot_id=fallback_id,
        ),
    )

    assert jobs._salesbot_id_for_channel("whatsapp") == expected


def test_private_message_salesbot_id_routing_requires_whatsapp_configuration(monkeypatch):
    from app.integrations.kommo import jobs
    from app.integrations.kommo.client import KommoAPIError

    monkeypatch.setattr(
        jobs,
        "get_config",
        lambda: SimpleNamespace(
            kommo_whatsapp_salesbot_id=None,
            kommo_salesbot_id=None,
        ),
    )

    with pytest.raises(KommoAPIError, match="not configured for whatsapp"):
        jobs._salesbot_id_for_channel("whatsapp")


def test_instagram_salesbot_routing_is_rejected(monkeypatch):
    from app.integrations.kommo import jobs
    from app.integrations.kommo.client import KommoAPIError

    monkeypatch.setattr(jobs, "get_config", lambda: SimpleNamespace())

    with pytest.raises(KommoAPIError, match="do not use Salesbot"):
        jobs._salesbot_id_for_channel("instagram")


@pytest.mark.asyncio
async def test_ready_job_discard_continues_salesbot_before_marking_discarded(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[{"id": "job"}, {"id": "job"}])
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)

    client = MagicMock()
    client.continue_salesbot = AsyncMock(return_value={"accepted": True})
    job = {"id": "job", "processing_lease_id": LEASE_ID, "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2"}

    await jobs._continue_and_discard_job(client, job, "global_ai_paused")

    client.continue_salesbot.assert_awaited_once()
    assert client.continue_salesbot.await_args.args == ("https://acme.kommo.com/api/v4/salesbot/1/continue/2",)
    assert client.continue_salesbot.await_args.kwargs == {
        "data": {"status": "fail", "message": ""},
    }
    assert mock_db.fetch_one.await_count == 2
    assert "status = 'continuing'" in mock_db.fetch_one.await_args_list[0].args[0]
    assert json.loads(mock_db.fetch_one.await_args_list[0].args[1]["continuation_payload"]) == {
        "data": {"status": "fail", "message": ""},
    }
    assert "status = 'discarded'" in mock_db.fetch_one.await_args_list[1].args[0]


@pytest.mark.asyncio
async def test_ready_job_discard_marks_unauthorized_continuation_failed(monkeypatch, caplog):
    from app.integrations.kommo import jobs
    from app.integrations.kommo.client import KommoAPIError

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value={"id": "job"})
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)

    client = MagicMock()
    client.continue_salesbot = AsyncMock(side_effect=KommoAPIError("Kommo API returned HTTP 401", status_code=401))
    job = {"id": "job", "processing_lease_id": LEASE_ID, "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2"}

    with caplog.at_level("WARNING", logger="app.integrations.kommo.jobs"):
        await jobs._continue_and_discard_job(client, job, "ai_run_error")

    client.continue_salesbot.assert_awaited_once()
    values = mock_db.execute.await_args_list[-1].args[1]
    assert values["status"] == "failed"
    assert values["last_error"] == "Kommo API returned HTTP 401"
    assert "Kommo failure continuation failed" in caplog.text
    assert "interaction_type=private_message" in caplog.text
    assert "Kommo API returned HTTP 401" in caplog.text


@pytest.mark.asyncio
async def test_pre_delivery_failure_continues_salesbot_with_safe_customer_message(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[{"id": "job"}, {"id": "job"}])
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)
    client = MagicMock()
    client.continue_salesbot = AsyncMock(return_value={"accepted": True})
    job = {
        "id": "job",
        "channel": "whatsapp",
        "processing_lease_id": LEASE_ID,
        "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
    }

    await jobs._continue_and_discard_job(
        client,
        job,
        "internal database password: secret",
        send_customer_fallback=True,
    )

    message = client.continue_salesbot.await_args.kwargs["data"]["message"]
    assert message == jobs.CUSTOMER_DELIVERY_FAILURE_MESSAGE
    assert "secret" not in message
    assert "database" not in message


@pytest.mark.asyncio
async def test_definitive_ready_job_failure_continues_salesbot_with_safe_customer_message(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[{"id": "job"}, {"id": "job"}])
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)
    client = MagicMock()
    client.continue_salesbot = AsyncMock(return_value={"accepted": True})
    job = {
        "id": "job",
        "channel": "whatsapp",
        "processing_lease_id": LEASE_ID,
        "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
    }

    await jobs._fail_ready_job(client, job, "internal transcription exception: secret")

    message = client.continue_salesbot.await_args.kwargs["data"]["message"]
    assert message == jobs.CUSTOMER_DELIVERY_FAILURE_MESSAGE
    assert "secret" not in message


@pytest.mark.asyncio
async def test_terminal_instagram_transcription_failure_uses_direct_fallback(monkeypatch):
    from app.integrations.kommo import jobs

    direct_fallback = AsyncMock()
    monkeypatch.setattr(jobs, "_continue_and_discard_job", direct_fallback)
    client = MagicMock()
    client.continue_salesbot = AsyncMock()
    job = {
        "id": "job",
        "channel": "instagram",
        "interaction_type": "private_message",
        "processing_lease_id": LEASE_ID,
        "talk_id": "300",
    }
    customer = {"id": "customer", "channel": "instagram"}

    await jobs._fail_ready_job(client, job, "transcription failed", customer=customer)

    direct_fallback.assert_awaited_once_with(
        client,
        job,
        "transcription failed",
        send_customer_fallback=True,
        customer=customer,
    )
    client.continue_salesbot.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_worker_cannot_issue_continuation_after_losing_lease(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value=None)
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)

    client = MagicMock()
    client.continue_salesbot = AsyncMock()
    await jobs._continue_and_discard_job(
        client,
        {
            "id": "job",
            "processing_lease_id": LEASE_ID,
            "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        },
        "stale_worker",
    )

    client.continue_salesbot.assert_not_awaited()
    mock_db.execute.assert_not_awaited()
    query, values = mock_db.fetch_one.await_args.args
    assert "status = 'processing'" in query
    assert "processing_lease_id = CAST(:processing_lease_id AS uuid)" in query
    assert "RETURNING id" in query
    assert values["processing_lease_id"] == LEASE_ID


@pytest.mark.asyncio
async def test_deferred_suppression_retry_cannot_continue_twice(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[{"id": "job"}, {"id": "job"}, None])
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)
    client = MagicMock()
    client.continue_salesbot = AsyncMock(return_value={"accepted": True})
    job = {
        "id": "job",
        "processing_lease_id": LEASE_ID,
        "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        "suppress_after_context": True,
        "automation_block_reason": "kommo_ai_mode_human",
    }

    await jobs._continue_and_discard_job(client, job, job["automation_block_reason"])
    await jobs._continue_and_discard_job(client, job, job["automation_block_reason"])

    client.continue_salesbot.assert_awaited_once_with(
        job["return_url"],
        data={"status": "fail", "message": ""},
    )
    assert all(
        "suppress_after_context =" not in call.args[0]
        and "automation_block_reason =" not in call.args[0]
        for call in mock_db.fetch_one.await_args_list
    )


@pytest.mark.asyncio
async def test_ready_job_sends_ai_reply_in_salesbot_data_message(monkeypatch, caplog):
    from app.integrations.kommo import jobs

    mock_db = _install_ready_job_db(monkeypatch, jobs, settings={"ai_enabled": True, "kommo_emoji_mode_whatsapp": "preserve"})
    monkeypatch.setattr(jobs, "get_config", lambda: SimpleNamespace(kommo_ai_active_enum_id=1))
    monkeypatch.setattr(jobs, "sync_local_state_from_ai_mode", AsyncMock())
    monkeypatch.setattr(
        jobs,
        "evaluate_automation_state",
        lambda **_kwargs: SimpleNamespace(allowed=True, reason=None, needs_ai_mode_initialization=False),
    )
    monkeypatch.setattr(
        jobs,
        "resolve_customer_from_kommo_job",
        AsyncMock(return_value={"id": "customer", "conversation_state": "active"}),
    )
    reply = "Aquí tienes el catálogo: https://store.example/static/catalog/catalog.pdf 💕"
    monkeypatch.setattr(jobs, "generate_response", AsyncMock(return_value={"text": reply, "escalated": False}))
    monkeypatch.setattr(jobs, "upsert_mapping", AsyncMock())

    client = MagicMock()
    client.continue_salesbot = AsyncMock(return_value={"accepted": True})
    client.send_talk_message = AsyncMock()
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)

    with caplog.at_level("INFO", logger="app.integrations.kommo.jobs"):
        await jobs._process_ready_job({
            "id": "job",
            "processing_lease_id": LEASE_ID,
            "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
            "combined_message": "Catalogo",
            "channel": "whatsapp",
            "correlation_id": "corr",
        })

    client.continue_salesbot.assert_awaited_once()
    assert jobs.generate_response.await_args.kwargs["integration_context"]["interaction_type"] == "private_message"
    assert jobs.generate_response.await_args.kwargs["message_source_id"] == "kommo-job:job"
    assert client.continue_salesbot.await_args.kwargs == {
        "data": {"status": "success", "delivery_mode": "salesbot", "message": reply},
    }
    client.send_talk_message.assert_not_awaited()
    continuation_payload = json.loads(_continuation_values(mock_db)["continuation_payload"])
    assert continuation_payload == {
        "data": {"status": "success", "delivery_mode": "salesbot", "message": reply}
    }
    assert "https://store.example/static/catalog/catalog.pdf" in continuation_payload["data"]["message"]
    assert "Aquí" in continuation_payload["data"]["message"]
    assert "💕" in continuation_payload["data"]["message"]
    assert "attachment_type" not in continuation_payload["data"]
    assert any("assistant_message_persisted_at" in call.args[0] for call in mock_db.execute.await_args_list)
    assert "Kommo Salesbot continuation succeeded: job_id=job interaction_type=private_message" in caplog.text


def _install_voice_ready_job_dependencies(monkeypatch, jobs, *, allowed=True):
    mock_db = _install_ready_job_db(
        monkeypatch,
        jobs,
        settings={"ai_enabled": True, "kommo_emoji_mode_whatsapp": "preserve"},
    )
    monkeypatch.setattr(jobs, "get_config", lambda: SimpleNamespace(kommo_ai_active_enum_id=1))
    monkeypatch.setattr(jobs, "sync_local_state_from_ai_mode", AsyncMock())
    monkeypatch.setattr(
        jobs,
        "evaluate_automation_state",
        lambda **_kwargs: SimpleNamespace(
            allowed=allowed,
            reason=None if allowed else "global_ai_paused",
            needs_ai_mode_initialization=False,
        ),
    )
    monkeypatch.setattr(
        jobs,
        "resolve_customer_from_kommo_job",
        AsyncMock(return_value={"id": "customer", "conversation_state": "active"}),
    )
    monkeypatch.setattr(jobs, "upsert_mapping", AsyncMock())
    client = MagicMock()
    client.continue_salesbot = AsyncMock(return_value={"accepted": True})
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)
    return mock_db, client


def _voice_ready_job(**overrides):
    job = {
        "id": "voice-job",
        "processing_lease_id": LEASE_ID,
        "attempt_count": 1,
        "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        "external_message_id": "voice-message-1",
        "combined_message": "[Kommo voice:voice-message-1 pendiente de transcripcion]",
        "message_type": "voice",
        "media_url": "https://media.example/voice.ogg?signature=secret",
        "inbound_attachments": [{
            "external_message_id": "voice-message-1",
            "message_type": "voice",
            "media_url": "https://media.example/voice.ogg?signature=secret",
        }],
        "channel": "whatsapp",
        "correlation_id": "corr",
    }
    job.update(overrides)
    return job


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job", "expected_message", "expected_image_url"),
    [
        (
            {
                "combined_message": (
                    "El cliente envio una imagen por Kommo.\n"
                    "[Kommo voice:voice-1 pendiente de transcripcion]"
                ),
                "message_type": "voice",
                "media_url": "https://media.example/voice.ogg",
                "inbound_attachments": [
                    {
                        "external_message_id": "image-1",
                        "message_type": "image",
                        "media_url": "https://media.example/image.jpg",
                    },
                    {
                        "external_message_id": "voice-1",
                        "message_type": "voice",
                        "media_url": "https://media.example/voice.ogg",
                    },
                ],
            },
            "El cliente envio una imagen por Kommo.\nQuiero esa pijama",
            "https://media.example/image.jpg",
        ),
        (
            {
                "combined_message": (
                    "[Kommo voice:voice-1 pendiente de transcripcion]\n"
                    "El cliente envio una imagen por Kommo."
                ),
                "message_type": "image",
                "media_url": "https://media.example/image.jpg",
                "inbound_attachments": [
                    {
                        "external_message_id": "voice-1",
                        "message_type": "voice",
                        "media_url": "https://media.example/voice.ogg",
                    },
                    {
                        "external_message_id": "image-1",
                        "message_type": "image",
                        "media_url": "https://media.example/image.jpg",
                    },
                ],
            },
            "Quiero esa pijama\nEl cliente envio una imagen por Kommo.",
            "https://media.example/image.jpg",
        ),
        (
            {
                "combined_message": (
                    "El cliente envio una imagen por Kommo.\n"
                    "[Kommo voice:voice-1 pendiente de transcripcion]\n"
                    "¿La tienen disponible?"
                ),
                "message_type": "voice",
                "media_url": "https://media.example/voice.ogg",
                "inbound_attachments": [
                    {
                        "external_message_id": "image-1",
                        "message_type": "picture",
                        "media_url": "https://media.example/image.jpg",
                    },
                    {
                        "external_message_id": "voice-1",
                        "message_type": "voice",
                        "media_url": "https://media.example/voice.ogg",
                    },
                ],
            },
            "El cliente envio una imagen por Kommo.\nQuiero esa pijama\n¿La tienen disponible?",
            "https://media.example/image.jpg",
        ),
        (
            {
                "combined_message": (
                    "El cliente envio una imagen por Kommo.\n"
                    "El cliente envio una imagen por Kommo.\n"
                    "[Kommo voice:voice-1 pendiente de transcripcion]"
                ),
                "message_type": "voice",
                "media_url": "https://media.example/voice.ogg",
                "inbound_attachments": [
                    {
                        "external_message_id": "image-1",
                        "message_type": "image",
                        "media_url": "https://media.example/image-1.jpg",
                    },
                    {
                        "external_message_id": "image-2",
                        "message_type": "picture",
                        "media_url": "https://media.example/image-2.jpg",
                    },
                    {
                        "external_message_id": "voice-1",
                        "message_type": "voice",
                        "media_url": "https://media.example/voice.ogg",
                    },
                ],
            },
            (
                "El cliente envio una imagen por Kommo.\n"
                "El cliente envio una imagen por Kommo.\n"
                "Quiero esa pijama"
            ),
            "https://media.example/image-2.jpg",
        ),
        (
            {
                "combined_message": (
                    "[Kommo voice:voice-1 pendiente de transcripcion]\n"
                    "El cliente envio una imagen por Kommo.\n"
                    "El cliente envio una imagen por Kommo."
                ),
                "message_type": "picture",
                "media_url": "https://media.example/image-2.jpg",
                "inbound_attachments": [
                    {
                        "external_message_id": "voice-1",
                        "message_type": "voice",
                        "media_url": "https://media.example/voice.ogg",
                    },
                    {
                        "external_message_id": "image-1",
                        "message_type": "image",
                        "media_url": "https://media.example/image-1.jpg",
                    },
                    {
                        "external_message_id": "image-2",
                        "message_type": "picture",
                        "media_url": "https://media.example/image-2.jpg",
                    },
                ],
            },
            (
                "Quiero esa pijama\n"
                "El cliente envio una imagen por Kommo.\n"
                "El cliente envio una imagen por Kommo."
            ),
            "https://media.example/image-2.jpg",
        ),
    ],
    ids=[
        "image-voice",
        "voice-image",
        "image-voice-text",
        "image-image-voice",
        "voice-image-image",
    ],
)
async def test_ready_mixed_media_preserves_transcription_and_image_context(
    monkeypatch,
    job,
    expected_message,
    expected_image_url,
):
    from app.integrations.kommo import jobs

    _, client = _install_voice_ready_job_dependencies(monkeypatch, jobs)
    transcribe = AsyncMock(return_value="Quiero esa pijama")
    monkeypatch.setattr(jobs, "transcribe_audio_url", transcribe)
    monkeypatch.setattr(
        jobs,
        "generate_response",
        AsyncMock(return_value={"text": "Sí, la reviso.", "escalated": False}),
    )

    await jobs._process_ready_job(_voice_ready_job(**job))

    transcribe.assert_awaited_once_with("https://media.example/voice.ogg")
    generated = jobs.generate_response.await_args.kwargs
    assert generated["message_text"] == expected_message
    assert generated["media_url"] == expected_image_url
    assert generated["integration_context"]["media_url_is_direct"] is True
    client.continue_salesbot.assert_awaited_once()


def test_invalid_inbound_attachment_metadata_uses_generalized_error():
    from app.ai.transcription import AudioTranscriptionError
    from app.integrations.kommo import jobs

    with pytest.raises(AudioTranscriptionError, match="Inbound attachment metadata is invalid"):
        jobs._job_inbound_attachments({"inbound_attachments": "not-json"})


def test_unsupported_kommo_attachment_is_not_sent_to_the_checkout_agent():
    from app.integrations.kommo import jobs

    assert jobs._has_unsupported_attachment({"message_type": "file"}) is True
    assert jobs._has_unsupported_attachment({"message_type": "video"}) is True
    assert jobs._has_unsupported_attachment({"message_type": "picture"}) is False
    assert jobs._has_unsupported_attachment({"message_type": "voice"}) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("combined_message", "expected"),
    [
        (
            "[Kommo voice:voice-1 pendiente de transcripcion]\n"
            "[Kommo audio:voice-2 pendiente de transcripcion]\n"
            "Y también quiero saber el precio",
            "Primera nota\nSegunda nota\nY también quiero saber el precio",
        ),
        (
            "Primero revisa el catálogo\n"
            "[Kommo voice:voice-1 pendiente de transcripcion]\n"
            "[Kommo audio:voice-2 pendiente de transcripcion]",
            "Primero revisa el catálogo\nPrimera nota\nSegunda nota",
        ),
    ],
    ids=["voice-voice-text", "text-voice-voice"],
)
async def test_multiple_voice_notes_preserve_debounce_arrival_order(monkeypatch, combined_message, expected):
    from app.integrations.kommo import jobs

    transcribe = AsyncMock(side_effect=["Primera nota", "Segunda nota"])
    monkeypatch.setattr(jobs, "transcribe_audio_url", transcribe)
    job = {
        "combined_message": combined_message,
        "message_type": "audio",
        "media_url": "https://media.example/voice-2.ogg",
        "inbound_attachments": [
            {
                "external_message_id": "voice-1",
                "message_type": "voice",
                "media_url": "https://media.example/voice-1.ogg?signature=one",
            },
            {
                "external_message_id": "voice-2",
                "message_type": "audio",
                "media_url": "https://media.example/voice-2.ogg?signature=two",
            },
        ],
    }

    result = await jobs._effective_customer_message(job)

    assert result == expected
    assert [call.args[0] for call in transcribe.await_args_list] == [
        "https://media.example/voice-1.ogg?signature=one",
        "https://media.example/voice-2.ogg?signature=two",
    ]


@pytest.mark.asyncio
async def test_duplicate_attachment_metadata_is_transcribed_once(monkeypatch):
    from app.integrations.kommo import jobs

    transcribe = AsyncMock(return_value="Una sola nota")
    monkeypatch.setattr(jobs, "transcribe_audio_url", transcribe)
    attachment = {
        "external_message_id": "voice-1",
        "message_type": "voice",
        "media_url": "https://media.example/voice-1.ogg",
    }

    result = await jobs._effective_customer_message({
        "combined_message": "[Kommo voice:voice-1 pendiente de transcripcion]",
        "inbound_attachments": [attachment, attachment],
    })

    assert result == "Una sola nota"
    transcribe.assert_awaited_once()


@pytest.mark.asyncio
async def test_ready_voice_job_transcribes_before_existing_salesbot_flow(monkeypatch):
    from app.integrations.kommo import jobs

    _, client = _install_voice_ready_job_dependencies(monkeypatch, jobs)
    transcribe = AsyncMock(return_value="Quiero una pijama rosada")
    monkeypatch.setattr(jobs, "transcribe_audio_url", transcribe)
    monkeypatch.setattr(
        jobs,
        "generate_response",
        AsyncMock(return_value={"text": "Claro, te muestro opciones.", "escalated": False}),
    )

    await jobs._process_ready_job(_voice_ready_job())

    transcribe.assert_awaited_once()
    assert jobs.generate_response.await_args.kwargs["message_text"] == "Quiero una pijama rosada"
    assert jobs.generate_response.await_args.kwargs["media_url"] is None
    assert jobs.generate_response.await_args.kwargs["message_source_id"] == "kommo-job:voice-job"
    client.continue_salesbot.assert_awaited_once()
    assert client.continue_salesbot.await_args.kwargs["data"]["message"] == "Claro, te muestro opciones."


@pytest.mark.asyncio
async def test_ready_text_job_does_not_invoke_transcription(monkeypatch):
    from app.integrations.kommo import jobs

    _, client = _install_voice_ready_job_dependencies(monkeypatch, jobs)
    transcribe = AsyncMock()
    monkeypatch.setattr(jobs, "transcribe_audio_url", transcribe)
    monkeypatch.setattr(
        jobs,
        "generate_response",
        AsyncMock(return_value={"text": "Si tenemos.", "escalated": False}),
    )

    await jobs._process_ready_job(_voice_ready_job(
        combined_message="Tienen pijamas?",
        message_type="text",
        media_url=None,
        inbound_attachments=[],
    ))

    transcribe.assert_not_awaited()
    assert jobs.generate_response.await_args.kwargs["message_text"] == "Tienen pijamas?"
    client.continue_salesbot.assert_awaited_once()


@pytest.mark.asyncio
async def test_ready_voice_job_missing_url_fails_without_ai_reply(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db, client = _install_voice_ready_job_dependencies(monkeypatch, jobs)
    monkeypatch.setattr(jobs, "generate_response", AsyncMock())

    await jobs._process_ready_job(_voice_ready_job(
        media_url=None,
        inbound_attachments=[{
            "external_message_id": "voice-message-1",
            "message_type": "voice",
            "media_url": None,
        }],
    ))

    jobs.generate_response.assert_not_awaited()
    client.continue_salesbot.assert_awaited_once_with(
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        data={"status": "fail", "message": jobs.CUSTOMER_DELIVERY_FAILURE_MESSAGE},
    )
    assert any(
        call.args[1].get("status") == "failed"
        and call.args[1].get("last_error") == "Audio attachment URL is missing"
        for call in mock_db.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_retryable_voice_failure_is_sanitized_and_requeued(monkeypatch):
    from app.ai.transcription import AudioTranscriptionError
    from app.integrations.kommo import jobs

    mock_db, client = _install_voice_ready_job_dependencies(monkeypatch, jobs)
    signed_url = "https://media.example/voice.ogg?access_token=secret"
    monkeypatch.setattr(
        jobs,
        "transcribe_audio_url",
        AsyncMock(side_effect=AudioTranscriptionError(f"download failed {signed_url}", retryable=True)),
    )
    monkeypatch.setattr(jobs, "generate_response", AsyncMock())

    await jobs._process_ready_job(_voice_ready_job(
        media_url=signed_url,
        inbound_attachments=[{
            "external_message_id": "voice-message-1",
            "message_type": "voice",
            "media_url": signed_url,
        }],
        attempt_count=1,
    ))

    jobs.generate_response.assert_not_awaited()
    client.continue_salesbot.assert_not_awaited()
    retry_call = next(call for call in mock_db.execute.await_args_list if "SET status = 'ready'" in call.args[0])
    assert signed_url not in retry_call.args[1]["last_error"]
    assert "secret" not in retry_call.args[1]["last_error"]


@pytest.mark.asyncio
async def test_suppressed_voice_job_persists_transcription_once(monkeypatch):
    from app.integrations.kommo import jobs

    _, client = _install_voice_ready_job_dependencies(monkeypatch, jobs, allowed=False)
    monkeypatch.setattr(jobs, "transcribe_audio_url", AsyncMock(return_value="Necesito hablar con alguien"))
    monkeypatch.setattr(jobs, "generate_response", AsyncMock())

    await jobs._process_ready_job(_voice_ready_job())

    jobs.generate_response.assert_not_awaited()
    jobs.conversations.store_message.assert_awaited_once()
    persisted = jobs.conversations.store_message.await_args.kwargs
    assert persisted["content"] == "Necesito hablar con alguien"
    assert "pendiente de transcripcion" not in persisted["content"]
    assert persisted["source_id"] == "kommo-job:voice-job"
    client.continue_salesbot.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ai_result", "delivered_attachments", "expected_text"),
    [
        (
            {
                "text": "Aqui tienes la foto. https://cdn.example/private-product.jpg",
                "product_image": {
                    "type": "product_image",
                    "caption": "Aqui tienes la foto.",
                    "image_url": "https://cdn.example/private-product.jpg",
                    "product_name": "Pijama Satin",
                    "sku": "PJ-1",
                },
                "escalated": False,
            },
            [{"type": "product_image", "product_name": "Pijama Satin", "sku": "PJ-1"}],
            "Aqui tienes la foto.",
        ),
        (
            {
                "text": "",
                "catalog_pdf": {
                    "type": "catalog_pdf",
                    "caption": "Aqui tienes el catalogo.",
                    "filename": "catalog.pdf",
                    "catalog_fingerprint": "catalog-v1",
                },
                "escalated": False,
            },
            [
                {
                    "type": "catalog_pdf",
                    "filename": "catalog.pdf",
                    "catalog_fingerprint": "catalog-v1",
                }
            ],
            "Aqui tienes el catalogo.",
        ),
    ],
)
async def test_ready_job_native_media_uses_chats_mode_and_persists_semantic_attachment(
    monkeypatch,
    ai_result,
    delivered_attachments,
    expected_text,
):
    from app.integrations.kommo import jobs
    from app.integrations.kommo.delivery import DeliveryResult

    mock_db = _install_ready_job_db(
        monkeypatch,
        jobs,
        settings={"ai_enabled": True, "kommo_emoji_mode_whatsapp": "preserve"},
    )
    monkeypatch.setattr(
        jobs,
        "get_config",
        lambda: SimpleNamespace(
            kommo_ai_active_enum_id=1,
            kommo_chats_media_enabled=True,
            kommo_chats_product_images_enabled=True,
            kommo_chats_catalog_pdf_enabled=True,
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
        AsyncMock(return_value={"id": "customer", "conversation_state": "active"}),
    )
    monkeypatch.setattr(jobs, "generate_response", AsyncMock(return_value=ai_result))
    monkeypatch.setattr(jobs, "upsert_mapping", AsyncMock())
    deliver = AsyncMock(
        return_value=DeliveryResult(
            transport="chats_api",
            customer_text=expected_text,
            delivered_attachments=delivered_attachments,
            provider_message_ids=["provider-message-1"],
        )
    )
    monkeypatch.setattr(jobs, "deliver_response", deliver)
    stored_messages = []
    events = []

    async def store_message(**kwargs):
        events.append("history")
        stored_messages.append(kwargs)

    monkeypatch.setattr(
        jobs.conversations,
        "store_message",
        AsyncMock(side_effect=store_message),
    )

    client = MagicMock()

    async def continue_salesbot(*_args, **_kwargs):
        events.append("continue")
        return {"accepted": True}

    client.continue_salesbot = AsyncMock(side_effect=continue_salesbot)
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)
    job = {
        "id": "job",
        "processing_lease_id": LEASE_ID,
        "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        "combined_message": "Enviamelo",
        "channel": "whatsapp",
        "talk_id": "105",
        "correlation_id": "corr",
    }

    await jobs._process_ready_job(job)

    deliver.assert_awaited_once()
    assert deliver.await_args.kwargs["customer_text"] == expected_text
    assert "https://cdn.example/private-product.jpg" not in expected_text
    client.continue_salesbot.assert_awaited_once_with(
        job["return_url"],
        data={"status": "success", "delivery_mode": "chats_api", "message": ""},
    )
    assert stored_messages == [
        {
            "customer_id": "customer",
            "role": "assistant",
            "content": expected_text,
            "channel": "whatsapp",
            "function_calls": None,
            "source_id": "kommo-job:job",
            "attachments": delivered_attachments,
            "interaction_type": "private_message",
        }
    ]
    assert events == ["history", "continue"]
    assert any("status = 'sent'" in call.args[0] for call in mock_db.fetch_one.await_args_list)


@pytest.mark.asyncio
async def test_media_history_persists_when_mark_continuing_loses_lease(monkeypatch):
    from app.integrations.kommo import jobs
    from app.integrations.kommo.delivery import DeliveryResult

    _install_ready_job_db(monkeypatch, jobs)
    monkeypatch.setattr(jobs, "get_config", lambda: SimpleNamespace(kommo_ai_active_enum_id=1))
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
        AsyncMock(return_value={"id": "customer", "conversation_state": "active"}),
    )
    ai_result = {
        "text": "Foto",
        "product_image": {
            "type": "product_image",
            "caption": "Foto",
            "image_url": "https://cdn.example/product.jpg",
        },
        "escalated": False,
    }
    monkeypatch.setattr(jobs, "generate_response", AsyncMock(return_value=ai_result))
    monkeypatch.setattr(jobs, "upsert_mapping", AsyncMock())
    attachments = [{"type": "product_image", "product_name": "Pijama"}]
    deliver = AsyncMock(
        return_value=DeliveryResult(
            transport="chats_api",
            customer_text="Foto",
            delivered_attachments=attachments,
            provider_message_ids=["provider-message-1"],
        )
    )
    monkeypatch.setattr(jobs, "deliver_response", deliver)
    mark_continuing = AsyncMock(return_value=False)
    monkeypatch.setattr(jobs, "_mark_job_continuing", mark_continuing)
    store_message = AsyncMock()
    monkeypatch.setattr(jobs.conversations, "store_message", store_message)
    client = MagicMock()
    client.continue_salesbot = AsyncMock()
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)

    await jobs._process_ready_job(
        {
            "id": "job",
            "processing_lease_id": LEASE_ID,
            "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
            "combined_message": "Foto",
            "channel": "whatsapp",
            "talk_id": "105",
            "correlation_id": "corr",
        }
    )

    deliver.assert_awaited_once()
    store_message.assert_awaited_once()
    assert store_message.await_args.kwargs["content"] == "Foto"
    assert store_message.await_args.kwargs["attachments"] == attachments
    assert store_message.await_args.kwargs["source_id"] == "kommo-job:job"
    mark_continuing.assert_awaited_once()
    client.continue_salesbot.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_image_delivery_persists_only_image_and_never_falls_back(monkeypatch):
    from app.integrations.kommo import jobs
    from app.integrations.kommo.delivery import KommoPartialDeliveryError

    mock_db = _install_ready_job_db(monkeypatch, jobs)
    monkeypatch.setattr(jobs, "get_config", lambda: SimpleNamespace(kommo_ai_active_enum_id=1))
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
        AsyncMock(return_value={"id": "customer", "conversation_state": "active"}),
    )
    ai_result = {
        "text": "Imagen y catalogo",
        "product_image": {
            "type": "product_image",
            "caption": "Imagen",
            "image_url": "https://cdn.example/product.jpg",
        },
        "catalog_pdf": {"type": "catalog_pdf", "caption": "Catalogo"},
        "escalated": False,
    }
    monkeypatch.setattr(jobs, "generate_response", AsyncMock(return_value=ai_result))
    monkeypatch.setattr(jobs, "upsert_mapping", AsyncMock())
    image_attachment = {"type": "product_image", "product_name": "Pijama"}
    partial = KommoPartialDeliveryError(
        "PDF delivery failed after image acceptance",
        customer_text="Imagen y catalogo",
        delivered_attachments=[image_attachment],
        provider_message_ids=["image-message-id"],
    )
    monkeypatch.setattr(jobs, "deliver_response", AsyncMock(side_effect=partial))
    store_message = AsyncMock()
    monkeypatch.setattr(jobs.conversations, "store_message", store_message)
    client = MagicMock()
    client.continue_salesbot = AsyncMock()
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)

    await jobs._process_ready_job(
        {
            "id": "job",
            "processing_lease_id": LEASE_ID,
            "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
            "combined_message": "Imagen y PDF",
            "channel": "whatsapp",
            "talk_id": "105",
            "correlation_id": "corr",
        }
    )

    store_message.assert_awaited_once()
    assert store_message.await_args.kwargs["content"] == "Imagen y catalogo"
    assert store_message.await_args.kwargs["attachments"] == [image_attachment]
    assert all(
        attachment["type"] != "catalog_pdf"
        for attachment in store_message.await_args.kwargs["attachments"]
    )
    assert mock_db.execute.await_args.args[1]["status"] == "delivery_unknown"
    client.continue_salesbot.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_job_ambiguous_media_send_never_continues_salesbot_fallback(monkeypatch):
    from app.integrations.kommo import jobs
    from app.integrations.kommo.delivery import KommoDeliveryUnknownError

    mock_db = _install_ready_job_db(monkeypatch, jobs)
    monkeypatch.setattr(jobs, "get_config", lambda: SimpleNamespace(kommo_ai_active_enum_id=1))
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
        AsyncMock(return_value={"id": "customer", "conversation_state": "active"}),
    )
    monkeypatch.setattr(
        jobs,
        "generate_response",
        AsyncMock(
            return_value={
                "text": "Foto",
                "product_image": {
                    "type": "product_image",
                    "caption": "Foto",
                    "image_url": "https://cdn.example/product.jpg",
                },
                "escalated": False,
            }
        ),
    )
    monkeypatch.setattr(jobs, "upsert_mapping", AsyncMock())
    monkeypatch.setattr(
        jobs,
        "deliver_response",
        AsyncMock(side_effect=KommoDeliveryUnknownError("connection lost")),
    )
    client = MagicMock()
    client.continue_salesbot = AsyncMock()
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)

    await jobs._process_ready_job(
        {
            "id": "job",
            "processing_lease_id": LEASE_ID,
            "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
            "combined_message": "Foto",
            "channel": "whatsapp",
            "talk_id": "105",
            "correlation_id": "corr",
        }
    )

    client.continue_salesbot.assert_not_awaited()
    assert mock_db.execute.await_args.args[1]["status"] == "delivery_unknown"


@pytest.mark.asyncio
async def test_ready_job_pre_delivery_media_exception_sends_safe_fallback(monkeypatch):
    from app.integrations.kommo import jobs

    _install_ready_job_db(monkeypatch, jobs)
    monkeypatch.setattr(jobs, "get_config", lambda: SimpleNamespace(kommo_ai_active_enum_id=1))
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
        AsyncMock(return_value={"id": "customer", "conversation_state": "active"}),
    )
    monkeypatch.setattr(
        jobs,
        "generate_response",
        AsyncMock(
            return_value={
                "text": "Foto",
                "product_image": {
                    "type": "product_image",
                    "caption": "Foto",
                    "image_url": "https://cdn.example/product.jpg",
                },
                "escalated": False,
            }
        ),
    )
    monkeypatch.setattr(jobs, "upsert_mapping", AsyncMock())
    monkeypatch.setattr(
        jobs,
        "deliver_response",
        AsyncMock(side_effect=RuntimeError("internal media storage error: secret")),
    )
    client = MagicMock()
    client.continue_salesbot = AsyncMock(return_value={"accepted": True})
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)

    await jobs._process_ready_job(
        {
            "id": "job",
            "processing_lease_id": LEASE_ID,
            "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
            "combined_message": "Foto",
            "channel": "whatsapp",
            "talk_id": "105",
            "correlation_id": "corr",
        }
    )

    message = client.continue_salesbot.await_args.kwargs["data"]["message"]
    assert message == jobs.CUSTOMER_DELIVERY_FAILURE_MESSAGE
    assert "secret" not in message


@pytest.mark.asyncio
async def test_media_history_is_persisted_before_salesbot_resume_failure(monkeypatch):
    from app.integrations.kommo import jobs
    from app.integrations.kommo.client import KommoAPIError
    from app.integrations.kommo.delivery import DeliveryResult

    mock_db = _install_ready_job_db(monkeypatch, jobs)
    monkeypatch.setattr(jobs, "get_config", lambda: SimpleNamespace(kommo_ai_active_enum_id=1))
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
        AsyncMock(return_value={"id": "customer", "conversation_state": "active"}),
    )
    ai_result = {
        "text": "Foto",
        "product_image": {
            "type": "product_image",
            "caption": "Foto",
            "image_url": "https://cdn.example/product.jpg",
        },
        "escalated": False,
    }
    monkeypatch.setattr(jobs, "generate_response", AsyncMock(return_value=ai_result))
    monkeypatch.setattr(jobs, "upsert_mapping", AsyncMock())
    attachments = [{"type": "product_image", "product_name": "Pijama"}]
    monkeypatch.setattr(
        jobs,
        "deliver_response",
        AsyncMock(
            return_value=DeliveryResult(
                transport="chats_api",
                customer_text="Foto",
                delivered_attachments=attachments,
                provider_message_ids=["provider-message-1"],
            )
        ),
    )
    events = []

    async def store_message_impl(**_kwargs):
        events.append("history")

    store_message = AsyncMock(side_effect=store_message_impl)
    monkeypatch.setattr(jobs.conversations, "store_message", store_message)
    client = MagicMock()

    async def fail_continuation(*_args, **_kwargs):
        events.append("continue")
        raise KommoAPIError("Kommo API returned HTTP 503", status_code=503)

    client.continue_salesbot = AsyncMock(side_effect=fail_continuation)
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)

    await jobs._process_ready_job(
        {
            "id": "job",
            "processing_lease_id": LEASE_ID,
            "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
            "combined_message": "Foto",
            "channel": "whatsapp",
            "talk_id": "105",
            "correlation_id": "corr",
        }
    )

    store_message.assert_awaited_once()
    assert store_message.await_args.kwargs["attachments"] == attachments
    assert events == ["history", "continue"]
    assert mock_db.execute.await_args.args[1]["status"] == "delivery_unknown"


@pytest.mark.asyncio
async def test_comment_ready_job_passes_interaction_type_to_ai_and_public_formatter(monkeypatch, caplog):
    from app.integrations.kommo import jobs

    mock_db = _install_ready_job_db(monkeypatch, jobs, settings={"ai_enabled": True, "kommo_emoji_mode_instagram": "preserve"})
    monkeypatch.setattr(
        jobs,
        "get_config",
        lambda: SimpleNamespace(kommo_ai_active_enum_id=1, kommo_ai_mode_field_id=10),
    )
    monkeypatch.setattr(jobs, "sync_local_state_from_ai_mode", AsyncMock())
    ensure_ai_mode = AsyncMock(return_value=(1, True))
    monkeypatch.setattr(jobs, "ensure_ai_mode_initialized", ensure_ai_mode)
    monkeypatch.setattr(
        jobs,
        "evaluate_automation_state",
        lambda **_kwargs: SimpleNamespace(allowed=True, reason=None, needs_ai_mode_initialization=False),
    )
    monkeypatch.setattr(
        jobs,
        "resolve_customer_from_kommo_job",
        AsyncMock(return_value={"id": "customer", "conversation_state": "active"}),
    )
    long_reply = "**Tenemos pijamas disponibles.**\n- Escríbenos por DM para tallas y compra. " + "x" * 400
    monkeypatch.setattr(jobs, "generate_response", AsyncMock(return_value={"text": long_reply, "escalated": False}))
    monkeypatch.setattr(jobs, "upsert_mapping", AsyncMock())

    client = MagicMock()
    client.get_lead = AsyncMock(return_value={"id": 100, "custom_fields_values": None})
    client.continue_salesbot = AsyncMock(return_value={"accepted": True})
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)

    with caplog.at_level("INFO", logger="app.integrations.kommo.jobs"):
        await jobs._process_ready_job({
            "id": "job",
            "processing_lease_id": LEASE_ID,
            "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
            "combined_message": "Precio?",
            "channel": "instagram",
            "interaction_type": "instagram_comment",
            "lead_id": "100",
            "public_comment_context": {
                "media_id": "media-1",
                "post_url": "https://www.instagram.com/p/ABC123/",
                "context_provider": "meta",
                "correlation_status": "matched",
            },
            "correlation_id": "corr",
        })

    assert jobs.generate_response.await_args.kwargs["integration_context"]["interaction_type"] == "instagram_comment"
    ensure_ai_mode.assert_awaited_once_with(client, "100", {"id": 100, "custom_fields_values": None})
    assert jobs.generate_response.await_args.kwargs["integration_context"]["public_comment_context"]["context_provider"] == "meta"
    message = client.continue_salesbot.await_args.kwargs["data"]["message"]
    assert "**" not in message
    assert "\n" not in message
    assert len(message) <= 300
    continuation_payload = json.loads(_continuation_values(mock_db)["continuation_payload"])
    assert continuation_payload["data"]["message"] == message
    assert "Kommo Salesbot continuation succeeded: job_id=job interaction_type=instagram_comment" in caplog.text


@pytest.mark.asyncio
async def test_ready_job_contact_fetch_failure_does_not_block_processing(monkeypatch):
    from app.integrations.kommo import jobs
    from app.integrations.kommo.client import KommoAPIError

    _install_ready_job_db(monkeypatch, jobs, settings={"ai_enabled": True, "kommo_emoji_mode_whatsapp": "preserve"})
    monkeypatch.setattr(jobs, "get_config", lambda: SimpleNamespace(kommo_ai_active_enum_id=1))
    monkeypatch.setattr(jobs, "sync_local_state_from_ai_mode", AsyncMock())
    monkeypatch.setattr(
        jobs,
        "evaluate_automation_state",
        lambda **_kwargs: SimpleNamespace(allowed=True, reason=None, needs_ai_mode_initialization=False),
    )
    monkeypatch.setattr(
        jobs,
        "resolve_customer_from_kommo_job",
        AsyncMock(return_value={"id": "customer", "conversation_state": "active"}),
    )
    monkeypatch.setattr(jobs, "generate_response", AsyncMock(return_value={"text": "Hola", "escalated": False}))
    monkeypatch.setattr(jobs, "upsert_mapping", AsyncMock())

    client = MagicMock()
    client.get_contact = AsyncMock(side_effect=KommoAPIError("Kommo API returned HTTP 500", status_code=500))
    client.continue_salesbot = AsyncMock(return_value={"accepted": True})
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)

    await jobs._process_ready_job({
        "id": "job",
        "processing_lease_id": LEASE_ID,
        "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        "combined_message": "Hola",
        "channel": "whatsapp",
        "contact_id": "200",
        "author_name": "Maria Cliente",
        "correlation_id": "corr",
    })

    client.get_contact.assert_awaited_once_with("200")
    jobs.generate_response.assert_awaited_once()
    assert jobs.generate_response.await_args.kwargs["customer_profile"]["display_name"] == "Maria Cliente"
    client.continue_salesbot.assert_awaited_once()


@pytest.mark.asyncio
async def test_contact_only_ready_job_without_resolved_lead_is_suppressed(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = _install_ready_job_db(monkeypatch, jobs, settings={"ai_enabled": True, "kommo_emoji_mode_whatsapp": "preserve"})
    monkeypatch.setattr(
        jobs,
        "get_config",
        lambda: SimpleNamespace(
            kommo_ai_active_enum_id=1,
            kommo_ai_human_enum_id=2,
            kommo_ai_paused_enum_id=3,
        ),
    )
    monkeypatch.setattr(jobs, "sync_local_state_from_ai_mode", AsyncMock())
    monkeypatch.setattr(
        jobs,
        "resolve_customer_from_kommo_job",
        AsyncMock(return_value={"id": "customer", "conversation_state": "active"}),
    )
    monkeypatch.setattr(jobs, "generate_response", AsyncMock(return_value={"text": "Hola", "escalated": False}))

    client = MagicMock()
    client.get_contact = AsyncMock(return_value={"id": 200, "_embedded": {"leads": []}})
    client.continue_salesbot = AsyncMock(return_value={"accepted": True})
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)

    await jobs._process_ready_job({
        "id": "job",
        "processing_lease_id": LEASE_ID,
        "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        "combined_message": "Hola",
        "channel": "whatsapp",
        "contact_id": "200",
        "correlation_id": "corr",
    })

    client.get_contact.assert_awaited_once_with("200")
    client.get_lead.assert_not_called()
    jobs.generate_response.assert_not_awaited()
    client.continue_salesbot.assert_awaited_once()
    assert client.continue_salesbot.await_args.kwargs["data"]["status"] == "fail"
    assert client.continue_salesbot.await_args.kwargs["data"]["message"] == ""
    assert any(
        values and values.get("last_error") == "kommo_ai_mode_empty"
        for _, values in (call.args for call in mock_db.fetch_one.await_args_list)
    )


@pytest.mark.asyncio
async def test_assistant_history_persisted_after_accepted_kommo_continuation(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = _install_ready_job_db(monkeypatch, jobs, settings={"ai_enabled": True, "kommo_emoji_mode_whatsapp": "preserve"})
    monkeypatch.setattr(jobs, "get_config", lambda: SimpleNamespace(kommo_ai_active_enum_id=1))
    monkeypatch.setattr(jobs, "sync_local_state_from_ai_mode", AsyncMock())
    monkeypatch.setattr(
        jobs,
        "evaluate_automation_state",
        lambda **_kwargs: SimpleNamespace(allowed=True, reason=None, needs_ai_mode_initialization=False),
    )
    monkeypatch.setattr(
        jobs,
        "resolve_customer_from_kommo_job",
        AsyncMock(return_value={"id": "customer", "conversation_state": "active"}),
    )
    monkeypatch.setattr(
        jobs,
        "generate_response",
        AsyncMock(
            return_value={
                "text": "**Listo** 💕",
                "escalated": False,
                "function_calls": [{"name": "check_inventory"}],
            }
        ),
    )
    monkeypatch.setattr(jobs, "upsert_mapping", AsyncMock())
    stored_messages = []
    events = []

    async def store_message(**kwargs):
        events.append("history")
        stored_messages.append(kwargs)

    monkeypatch.setattr(jobs.conversations, "store_message", AsyncMock(side_effect=store_message))

    client = MagicMock()

    async def continue_salesbot(*_args, **_kwargs):
        events.append("continue")
        return {"accepted": True}

    client.continue_salesbot = AsyncMock(side_effect=continue_salesbot)
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)

    await jobs._process_ready_job({
        "id": "job",
        "processing_lease_id": LEASE_ID,
        "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        "combined_message": "Gracias",
        "channel": "whatsapp",
        "correlation_id": "corr",
    })

    assert stored_messages == [
        {
            "customer_id": "customer",
            "role": "assistant",
            "content": "*Listo* 💕",
            "channel": "whatsapp",
            "function_calls": [{"name": "check_inventory"}],
            "source_id": "kommo-job:job",
            "attachments": None,
            "interaction_type": "private_message",
        }
    ]
    assert events == ["continue", "history"]
    assert any("status = 'sent'" in call.args[0] for call in mock_db.fetch_one.await_args_list)


@pytest.mark.asyncio
async def test_assistant_history_persistence_is_idempotent_per_kommo_job(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    mock_db.fetch_one = AsyncMock(return_value={"assistant_message_persisted_at": "2026-01-01T00:00:00Z"})
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)
    monkeypatch.setattr(jobs.conversations, "store_message", AsyncMock())

    await jobs._store_assistant_message_after_delivery(
        {"id": "customer", "channel": "whatsapp"},
        {"id": "job", "channel": "whatsapp"},
        {"function_calls": [{"name": "check_inventory"}]},
        "Respuesta final",
    )

    jobs.conversations.store_message.assert_not_awaited()
    mock_db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_previously_persisted_media_history_is_not_duplicated_on_retry(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    mock_db.fetch_one = AsyncMock(
        return_value={"assistant_message_persisted_at": "2026-01-01T00:00:00Z"}
    )
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)
    store_message = AsyncMock()
    monkeypatch.setattr(jobs.conversations, "store_message", store_message)

    for _ in range(2):
        await jobs._store_assistant_message_after_delivery(
            {"id": "customer", "channel": "whatsapp"},
            {"id": "job", "channel": "whatsapp"},
            {"function_calls": [{"name": "send_product_image"}]},
            "Foto enviada",
            delivered_attachments=[{"type": "product_image", "product_name": "Pijama"}],
            expected_job_status=None,
        )

    store_message.assert_not_awaited()
    mock_db.execute.assert_not_awaited()
    assert mock_db.fetch_one.await_count == 2
    assert all(
        "status =" not in call.args[0] and "processing_lease_id" not in call.args[0]
        for call in mock_db.fetch_one.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "result",
        "customer_text",
        "delivered_attachments",
        "expected_content",
        "expected_attachments",
    ),
    [
        (
            {
                "text": "Mira esta opción",
                "product_image": {
                    "type": "product_image",
                    "product_name": "Coconut Passion",
                    "sku": "VS-CP-01",
                    "image_url": "https://example.com/private.jpg",
                },
                "function_calls": [{"name": "send_product_image"}],
            },
            "Mira esta opción",
            [
                {
                    "type": "product_image",
                    "product_name": "Coconut Passion",
                    "sku": "VS-CP-01",
                }
            ],
            "Mira esta opción",
            [
                {
                    "type": "product_image",
                    "product_name": "Coconut Passion",
                    "sku": "VS-CP-01",
                }
            ],
        ),
        (
            {
                "text": "",
                "product_image": {
                    "type": "product_image",
                    "product_name": "Coconut Passion",
                    "image_url": "https://example.com/private.jpg",
                },
            },
            None,
            [{"type": "product_image", "product_name": "Coconut Passion"}],
            "",
            [{"type": "product_image", "product_name": "Coconut Passion"}],
        ),
        (
            {
                "text": None,
                "catalog_pdf": {
                    "type": "catalog_pdf",
                    "filename": "Catalogo Zona Pink.pdf",
                    "catalog_fingerprint": "catalog-v1",
                    "download_url": "https://example.com/private.pdf",
                },
            },
            None,
            [
                {
                    "type": "catalog_pdf",
                    "filename": "Catalogo Zona Pink.pdf",
                    "catalog_fingerprint": "catalog-v1",
                }
            ],
            "",
            [
                {
                    "type": "catalog_pdf",
                    "filename": "Catalogo Zona Pink.pdf",
                    "catalog_fingerprint": "catalog-v1",
                }
            ],
        ),
    ],
)
async def test_assistant_history_persists_semantic_media_turns(
    monkeypatch,
    result,
    customer_text,
    delivered_attachments,
    expected_content,
    expected_attachments,
):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    mock_db.fetch_one = AsyncMock(return_value={"assistant_message_persisted_at": None})
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)
    store_message = AsyncMock()
    monkeypatch.setattr(jobs.conversations, "store_message", store_message)

    await jobs._store_assistant_message_after_delivery(
        {"id": "customer", "channel": "whatsapp"},
        {"id": "job", "channel": "whatsapp", "processing_lease_id": LEASE_ID},
        result,
        customer_text,
        delivered_attachments=delivered_attachments,
    )

    store_message.assert_awaited_once()
    persisted = store_message.await_args.kwargs
    assert persisted["content"] == expected_content
    assert persisted["attachments"] == expected_attachments
    assert persisted["function_calls"] == result.get("function_calls")
    assert persisted["source_id"] == "kommo-job:job"
    assert persisted["interaction_type"] == "private_message"
    assert "image_url" not in str(persisted["attachments"])
    assert "download_url" not in str(persisted["attachments"])
    assert any("assistant_message_persisted_at = NOW()" in call.args[0] for call in mock_db.execute.await_args_list)


@pytest.mark.asyncio
async def test_salesbot_product_image_intent_persists_text_without_attachment(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    mock_db.fetch_one = AsyncMock(return_value={"assistant_message_persisted_at": None})
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)
    store_message = AsyncMock()
    monkeypatch.setattr(jobs.conversations, "store_message", store_message)
    result = {
        "text": "Aquí está la imagen: https://example.com/product.jpg",
        "product_image": {
            "type": "product_image",
            "product_name": "Coconut Passion",
            "sku": "VS-CP-01",
            "image_url": "https://example.com/product.jpg",
        },
    }

    await jobs._store_assistant_message_after_delivery(
        {"id": "customer", "channel": "whatsapp"},
        {"id": "job", "channel": "whatsapp", "processing_lease_id": LEASE_ID},
        result,
        result["text"],
        delivered_attachments=None,
    )

    persisted = store_message.await_args.kwargs
    assert persisted["content"] == result["text"]
    assert persisted["attachments"] is None


@pytest.mark.asyncio
async def test_public_comment_assistant_history_uses_comment_scope(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    mock_db.fetch_one = AsyncMock(return_value={"assistant_message_persisted_at": None})
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)
    store_message = AsyncMock()
    monkeypatch.setattr(jobs.conversations, "store_message", store_message)

    await jobs._store_assistant_message_after_delivery(
        {"id": "customer", "channel": "instagram"},
        {
            "id": "comment-job",
            "channel": "instagram",
            "interaction_type": "instagram_comment",
            "processing_lease_id": LEASE_ID,
        },
        {"text": "El precio es $71"},
        "El precio es $71",
    )

    assert store_message.await_args.kwargs["interaction_type"] == "instagram_comment"


def test_semantic_attachment_builder_keeps_only_transport_independent_fields():
    from app.integrations.kommo import jobs

    attachments = jobs._semantic_attachments_from_result(
        {
            "product_image": {
                "type": "product_image",
                "product_name": "Coconut Passion",
                "sku": "VS-CP-01",
                "image_url": "https://example.com/product.jpg",
                "drive_uuid": "hidden",
            },
            "catalog_pdf": {
                "type": "catalog_pdf",
                "filename": "Catalogo Zona Pink.pdf",
                "catalog_fingerprint": "catalog-v1",
                "download_url": "https://example.com/catalog.pdf",
            },
        }
    )

    assert attachments == [
        {
            "type": "product_image",
            "product_name": "Coconut Passion",
            "sku": "VS-CP-01",
        },
        {
            "type": "catalog_pdf",
            "filename": "Catalogo Zona Pink.pdf",
            "catalog_fingerprint": "catalog-v1",
        },
    ]


@pytest.mark.asyncio
async def test_repeated_media_assistant_persistence_writes_one_logical_row(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    mock_db.fetch_one = AsyncMock(
        side_effect=[
            {"assistant_message_persisted_at": None},
            {"assistant_message_persisted_at": "2026-08-04T00:00:00Z"},
        ]
    )
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)
    store_message = AsyncMock()
    monkeypatch.setattr(jobs.conversations, "store_message", store_message)
    job = {"id": "job", "channel": "whatsapp", "processing_lease_id": LEASE_ID}
    result = {
        "product_image": {"type": "product_image", "product_name": "Coconut Passion"}
    }

    await jobs._store_assistant_message_after_delivery(
        {"id": "customer"},
        job,
        result,
        None,
        delivered_attachments=[{"type": "product_image", "product_name": "Coconut Passion"}],
    )
    await jobs._store_assistant_message_after_delivery(
        {"id": "customer"},
        job,
        result,
        None,
        delivered_attachments=[{"type": "product_image", "product_name": "Coconut Passion"}],
    )

    store_message.assert_awaited_once()
    mock_db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_assistant_result_does_not_persist_history(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    mock_db.fetch_one = AsyncMock()
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)
    store_message = AsyncMock()
    monkeypatch.setattr(jobs.conversations, "store_message", store_message)

    await jobs._store_assistant_message_after_delivery(
        {"id": "customer"},
        {"id": "job", "processing_lease_id": LEASE_ID},
        {"text": "", "function_calls": [{"name": "check_inventory"}]},
        None,
    )

    store_message.assert_not_awaited()
    mock_db.fetch_one.assert_not_awaited()
    mock_db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_job_formats_and_strips_emoji_when_kommo_setting_enabled(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = _install_ready_job_db(monkeypatch, jobs, settings={"ai_enabled": True, "kommo_emoji_mode_whatsapp": "strip"})
    monkeypatch.setattr(jobs, "get_config", lambda: SimpleNamespace(kommo_ai_active_enum_id=1))
    monkeypatch.setattr(jobs, "sync_local_state_from_ai_mode", AsyncMock())
    monkeypatch.setattr(
        jobs,
        "evaluate_automation_state",
        lambda **_kwargs: SimpleNamespace(allowed=True, reason=None, needs_ai_mode_initialization=False),
    )
    monkeypatch.setattr(
        jobs,
        "resolve_customer_from_kommo_job",
        AsyncMock(return_value={"id": "customer", "conversation_state": "active"}),
    )
    monkeypatch.setattr(
        jobs,
        "generate_response",
        AsyncMock(return_value={"text": "¡Hola! **Promo especial** 💕", "escalated": False}),
    )
    monkeypatch.setattr(jobs, "upsert_mapping", AsyncMock())

    client = MagicMock()
    client.continue_salesbot = AsyncMock(return_value={"accepted": True})
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)

    await jobs._process_ready_job({
        "id": "job",
        "processing_lease_id": LEASE_ID,
        "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        "combined_message": "Promo",
        "channel": "whatsapp",
        "correlation_id": "corr",
    })

    assert client.continue_salesbot.await_args.kwargs == {
        "data": {
            "status": "success",
            "delivery_mode": "salesbot",
            "message": "¡Hola! *Promo especial*",
        },
    }
    continuation_payload = json.loads(_continuation_values(mock_db)["continuation_payload"])
    assert continuation_payload == {
        "data": {
            "status": "success",
            "delivery_mode": "salesbot",
            "message": "¡Hola! *Promo especial*",
        }
    }
    assert "?" not in continuation_payload["data"]["message"]


@pytest.mark.asyncio
async def test_ready_job_catalog_payload_never_adds_attachment_metadata(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = _install_ready_job_db(monkeypatch, jobs, settings={"ai_enabled": True, "kommo_emoji_mode_whatsapp": "preserve"})
    monkeypatch.setattr(jobs, "get_config", lambda: SimpleNamespace(kommo_ai_active_enum_id=1))
    monkeypatch.setattr(jobs, "sync_local_state_from_ai_mode", AsyncMock())
    monkeypatch.setattr(
        jobs,
        "evaluate_automation_state",
        lambda **_kwargs: SimpleNamespace(allowed=True, reason=None, needs_ai_mode_initialization=False),
    )
    monkeypatch.setattr(
        jobs,
        "resolve_customer_from_kommo_job",
        AsyncMock(return_value={"id": "customer", "conversation_state": "active"}),
    )
    reply = "Tenemos pijamas, sets y lencería. ¿Qué te gustaría ver primero?"
    monkeypatch.setattr(
        jobs,
        "generate_response",
        AsyncMock(return_value={"text": reply, "catalog_pdf": {"type": "catalog_pdf", "caption": "Catalogo"}, "escalated": False}),
    )
    monkeypatch.setattr(jobs, "upsert_mapping", AsyncMock())

    client = MagicMock()
    client.continue_salesbot = AsyncMock(return_value={"accepted": True})
    monkeypatch.setattr(jobs.KommoClient, "from_config", lambda: client)

    await jobs._process_ready_job({
        "id": "job",
        "processing_lease_id": LEASE_ID,
        "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        "combined_message": "Catalogo",
        "channel": "whatsapp",
        "correlation_id": "corr",
    })

    assert client.continue_salesbot.await_args.kwargs == {
        "data": {"status": "success", "delivery_mode": "salesbot", "message": reply},
    }
    continuation_payload = json.loads(_continuation_values(mock_db)["continuation_payload"])
    assert continuation_payload == {
        "data": {"status": "success", "delivery_mode": "salesbot", "message": reply}
    }
    assert "attachment_type" not in continuation_payload["data"]


@pytest.mark.asyncio
async def test_accepted_continuation_log_does_not_claim_delivery(monkeypatch, caplog):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value={"id": "job"})
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)

    with caplog.at_level("INFO", logger="app.integrations.kommo.jobs"):
        await jobs._mark_job_sent("job", LEASE_ID, {"accepted": True})

    assert "Kommo continuation accepted" in caplog.text
    assert "marked sent" not in caplog.text
    assert "confirmed" not in caplog.text.lower()
    assert "WhatsApp delivery" not in caplog.text


def test_continuation_prepared_log_is_structural_only(caplog):
    from app.integrations.kommo import jobs

    continuation_data = {"status": "success", "message": "Mensaje secreto del cliente"}
    with caplog.at_level("INFO", logger="app.integrations.kommo.jobs"):
        jobs._log_continuation_prepared("job", continuation_data)

    assert "message_present=True" in caplog.text
    assert f"message_length={len(continuation_data['message'])}" in caplog.text
    assert "channel=unknown" in caplog.text
    assert "newline_count=0" in caplog.text
    assert "non_ascii_present=False" in caplog.text
    assert "emoji_present=False" in caplog.text
    assert "replacement_char_present=False" in caplog.text
    assert "literal_question_mark_present=False" in caplog.text
    assert "kommo_emoji_mode=preserve" in caplog.text
    assert "kommo_strip_emoji_applied=False" in caplog.text
    assert "handler_count" not in caplog.text
    assert "status=success" in caplog.text
    assert "Mensaje secreto del cliente" not in caplog.text


@pytest.mark.asyncio
async def test_stale_job_recovery_runs_all_updates(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.execute = AsyncMock(return_value=1)
    monkeypatch.setattr(jobs, "db", mock_db)
    result = await jobs.recover_stale_jobs()
    assert mock_db.execute.await_count == 4
    assert result["failed_waiting"] == 1
    assert result["marked_delivery_unknown"] == 1
    waiting_query = mock_db.execute.await_args_list[0].args[0]
    assert "WHERE status = 'waiting_for_salesbot'" in waiting_query
    assert "COALESCE(salesbot_launched_at, updated_at, created_at)" in waiting_query
    reset_query = mock_db.execute.await_args_list[1].args[0]
    unknown_query = mock_db.execute.await_args_list[2].args[0]
    failed_query = mock_db.execute.await_args_list[3].args[0]
    assert "processing_lease_id = NULL" in reset_query
    assert "ai_started_at IS NULL" in reset_query
    assert "channel = 'instagram'" in reset_query
    assert "interaction_type = 'private_message'" in reset_query
    assert "talk_id IS NOT NULL" in reset_query
    assert "THEN 'ready'" in reset_query
    assert "status = 'processing' AND ai_started_at IS NOT NULL" in unknown_query
    assert "manual reconciliation required" in unknown_query
    assert "processing_lease_id = NULL" in unknown_query
    assert "ai_started_at IS NULL" in failed_query


@pytest.mark.asyncio
async def test_ready_job_claim_creates_fence_before_ai_side_effects(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value=None)
    monkeypatch.setattr(jobs, "db", mock_db)

    await jobs._claim_ready_job()

    query = mock_db.fetch_one.await_args.args[0]
    assert "processing_lease_id = gen_random_uuid()" in query
    assert "ai_started_at = NOW()" in query
    assert "candidate.return_url IS NOT NULL" in query
    assert "candidate.channel = 'instagram'" in query
    assert "candidate.interaction_type = 'private_message'" in query
    assert "candidate.talk_id IS NOT NULL" in query
    assert "FOR UPDATE SKIP LOCKED" in query


@pytest.mark.asyncio
async def test_broadcast_delivery_rejected_in_kommo_mode(monkeypatch):
    from app.broadcast import sender

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value={"id": "b1", "status": "draft"})
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(sender, "db", mock_db)
    monkeypatch.setattr(sender, "get_config", lambda: SimpleNamespace(channel_backend="kommo"))

    result = await sender.execute_broadcast("b1")
    assert "CHANNEL_BACKEND=kommo" in result["error"]
    mock_db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduled_broadcast_error_marks_failed(monkeypatch):
    from app.broadcast import scheduler

    mock_db = MagicMock()
    mock_db.fetch_all = AsyncMock(return_value=[{"id": "b1", "name": "Promo"}])
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(scheduler, "db", mock_db)
    monkeypatch.setattr(
        scheduler,
        "execute_broadcast",
        AsyncMock(return_value={"error": "WhatsApp broadcast delivery is unavailable"}),
    )
    monkeypatch.setattr(scheduler, "recover_stale_broadcast_deliveries", AsyncMock(return_value={}))

    await scheduler._check_scheduled_broadcasts()

    update_query = mock_db.execute.await_args.args[0]
    assert "status = 'failed'" in update_query
    assert "status = 'scheduled'" in update_query
