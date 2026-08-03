import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
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
    mock_db.fetch_one = AsyncMock(
        side_effect=[
            {"conversation_state": "active"},
            {"id": "job"},
            {"assistant_message_persisted_at": assistant_persisted_at},
            {"id": "job"},
        ]
    )
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)
    monkeypatch.setattr(jobs.conversations, "store_message", AsyncMock())
    return mock_db


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
        self.superseded_private_jobs = superseded_private_jobs or []
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
        if "UPDATE kommo_message_jobs job" in query and "interaction_type = 'private_message'" in query:
            return self.superseded_private_jobs
        return []


class _LaunchSafetyDB:
    def __init__(self, *, comment_match=None):
        self.comment_match = comment_match
        self.fetch_one_calls = []
        self.execute_calls = []

    async def fetch_one(self, query, values=None):
        supplied = set((values or {}).keys())
        expected = _bind_names(query)
        assert supplied == expected
        self.fetch_one_calls.append((query, dict(values or {})))
        if "interaction_type = 'instagram_comment'" in query:
            return self.comment_match
        if "status = 'waiting_for_salesbot'" in query:
            return {"id": (values or {}).get("id"), "status": "waiting_for_salesbot", "lead_id": "100", "contact_id": "200", "channel": "instagram", "combined_message": "Precio?"}
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
async def test_debounce_merges_rapid_messages(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[None, None, {"id": "pending-id", "combined_message": "Hola"}])
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
    assert "active.status IN ('prepared', 'waiting_for_salesbot', 'waiting_for_context', 'ready', 'processing', 'continuing')" in query
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
    assert "status IN ('pending', 'prepared')" in reconciliation_query
    assert reconciliation_values["reason"] == "superseded_by_instagram_comment"
    assert reconciliation_values["window_seconds"] == 30
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
    assert "status IN ('pending', 'prepared')" in reconciliation_query
    assert "FOR UPDATE SKIP LOCKED" in reconciliation_query
    assert reconciliation_values["normalized_message"] == "precio?"
    assert reconciliation_values["window_seconds"] == jobs.COMMENT_MIRROR_RECONCILIATION_SECONDS
    assert reconciliation_values["reason"] == "superseded_by_instagram_comment"


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
            kommo_instagram_dm_salesbot_id=555,
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
            kommo_instagram_dm_salesbot_id=701,
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

    await jobs._launch_salesbot_for_job(
        {
            "id": "job",
            "status": "processing",
            "processing_lease_id": LEASE_ID,
            "lead_id": "100",
            "combined_message": "Hola",
            "channel": channel,
            "interaction_type": "private_message",
        }
    )

    client.run_salesbot.assert_not_awaited()
    store_message.assert_awaited_once()
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

    await jobs._launch_salesbot_for_job(
        {
            "id": "private-job",
            "status": "processing",
            "lead_id": "100",
            "contact_id": "200",
            "combined_message": "Precio?",
            "channel": "instagram",
            "origin": "instagram_business",
            "interaction_type": "private_message",
        }
    )

    assert any("interaction_type = 'instagram_comment'" in query for query, _values in safety_db.fetch_one_calls)
    discard_values = safety_db.execute_calls[-1][1]
    assert discard_values["status"] == "discarded"
    assert discard_values["last_error"] == "superseded_by_instagram_comment"


@pytest.mark.asyncio
async def test_unrelated_instagram_dm_is_not_suppressed_by_comment_safety(monkeypatch):
    from app.integrations.kommo import jobs

    safety_db = _LaunchSafetyDB(comment_match=None)
    safety_db.get_settings = AsyncMock(return_value={"ai_enabled": True})
    monkeypatch.setattr(jobs, "db", safety_db)
    monkeypatch.setattr(
        jobs,
        "get_config",
        lambda: SimpleNamespace(
            kommo_instagram_dm_salesbot_id=701,
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

    await jobs._launch_salesbot_for_job(
        {
            "id": "private-job",
            "status": "processing",
            "lead_id": "100",
            "contact_id": "200",
            "combined_message": "Hola por DM",
            "channel": "instagram",
            "origin": "instagram_business",
            "interaction_type": "private_message",
        }
    )

    client.run_salesbot.assert_awaited_once_with("100", "leads", 701)
    assert not any(call[1].get("last_error") == "superseded_by_instagram_comment" for call in safety_db.execute_calls)


@pytest.mark.parametrize(
    ("channel", "instagram_id", "whatsapp_id", "fallback_id", "expected"),
    [
        ("instagram", 701, 702, 700, 701),
        ("whatsapp", 701, 702, 700, 702),
        ("instagram", None, 702, 700, 700),
        ("whatsapp", 701, None, 700, 700),
    ],
)
def test_private_message_salesbot_id_routing(
    monkeypatch,
    channel,
    instagram_id,
    whatsapp_id,
    fallback_id,
    expected,
):
    from app.integrations.kommo import jobs

    monkeypatch.setattr(
        jobs,
        "get_config",
        lambda: SimpleNamespace(
            kommo_instagram_dm_salesbot_id=instagram_id,
            kommo_whatsapp_salesbot_id=whatsapp_id,
            kommo_salesbot_id=fallback_id,
        ),
    )

    assert jobs._salesbot_id_for_channel(channel) == expected


@pytest.mark.parametrize("channel", ["instagram", "whatsapp"])
def test_private_message_salesbot_id_routing_requires_channel_configuration(monkeypatch, channel):
    from app.integrations.kommo import jobs
    from app.integrations.kommo.client import KommoAPIError

    monkeypatch.setattr(
        jobs,
        "get_config",
        lambda: SimpleNamespace(
            kommo_instagram_dm_salesbot_id=None,
            kommo_whatsapp_salesbot_id=None,
            kommo_salesbot_id=None,
        ),
    )

    with pytest.raises(KommoAPIError, match=f"not configured for {channel}"):
        jobs._salesbot_id_for_channel(channel)


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
    assert client.continue_salesbot.await_args.kwargs == {
        "data": {"status": "success", "message": reply},
    }
    continuation_payload = json.loads(_continuation_values(mock_db)["continuation_payload"])
    assert continuation_payload == {"data": {"status": "success", "message": reply}}
    assert "https://store.example/static/catalog/catalog.pdf" in continuation_payload["data"]["message"]
    assert "Aquí" in continuation_payload["data"]["message"]
    assert "💕" in continuation_payload["data"]["message"]
    assert "attachment_type" not in continuation_payload["data"]
    assert any("assistant_message_persisted_at" in call.args[0] for call in mock_db.execute.await_args_list)
    assert "Kommo Salesbot continuation succeeded: job_id=job interaction_type=private_message" in caplog.text


@pytest.mark.asyncio
async def test_comment_ready_job_passes_interaction_type_to_ai_and_public_formatter(monkeypatch, caplog):
    from app.integrations.kommo import jobs

    mock_db = _install_ready_job_db(monkeypatch, jobs, settings={"ai_enabled": True, "kommo_emoji_mode_instagram": "preserve"})
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
    long_reply = "**Tenemos pijamas disponibles.**\n- Escríbenos por DM para tallas y compra. " + "x" * 400
    monkeypatch.setattr(jobs, "generate_response", AsyncMock(return_value={"text": long_reply, "escalated": False}))
    monkeypatch.setattr(jobs, "upsert_mapping", AsyncMock())

    client = MagicMock()
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
            "public_comment_context": {
                "media_id": "media-1",
                "post_url": "https://www.instagram.com/p/ABC123/",
                "context_provider": "meta",
                "correlation_status": "matched",
            },
            "correlation_id": "corr",
        })

    assert jobs.generate_response.await_args.kwargs["integration_context"]["interaction_type"] == "instagram_comment"
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
    monkeypatch.setattr(jobs, "generate_response", AsyncMock(return_value={"text": "**Listo** 💕", "escalated": False}))
    monkeypatch.setattr(jobs, "upsert_mapping", AsyncMock())
    stored_messages = []

    async def store_message(**kwargs):
        stored_messages.append(kwargs)

    monkeypatch.setattr(jobs.conversations, "store_message", AsyncMock(side_effect=store_message))

    client = MagicMock()
    client.continue_salesbot = AsyncMock(return_value={"accepted": True})
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
            "function_calls": None,
        }
    ]
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
        "data": {"status": "success", "message": "¡Hola! *Promo especial*"},
    }
    continuation_payload = json.loads(_continuation_values(mock_db)["continuation_payload"])
    assert continuation_payload == {"data": {"status": "success", "message": "¡Hola! *Promo especial*"}}
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
        "data": {"status": "success", "message": reply},
    }
    continuation_payload = json.loads(_continuation_values(mock_db)["continuation_payload"])
    assert continuation_payload == {"data": {"status": "success", "message": reply}}
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
    assert "COALESCE(salesbot_launched_at, updated_at, created_at)" in mock_db.execute.await_args_list[0].args[0]
    reset_query = mock_db.execute.await_args_list[1].args[0]
    unknown_query = mock_db.execute.await_args_list[2].args[0]
    failed_query = mock_db.execute.await_args_list[3].args[0]
    assert "processing_lease_id = NULL" in reset_query
    assert "ai_started_at IS NULL" in reset_query
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
