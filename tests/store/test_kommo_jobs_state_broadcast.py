import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.integrations.kommo.models import NormalizedKommoEvent, SalesbotWidgetData
from app.integrations.kommo.state import evaluate_automation_state


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
    mock_db.fetch_one = AsyncMock(side_effect=[
        {"conversation_state": "active"},
        {"assistant_message_persisted_at": assistant_persisted_at},
    ])
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)
    monkeypatch.setattr(jobs.conversations, "store_message", AsyncMock())
    return mock_db


def _bind_names(query: str) -> set[str]:
    return set(re.findall(r":([A-Za-z_][A-Za-z0-9_]*)", query))


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
        author_type="external",
    )
    result = await jobs.record_incoming_event(event)
    assert result == {"status": "created", "job_id": "job-id"}
    assert "pg_advisory_xact_lock" in mock_db.fetch_one.await_args_list[0].args[0]
    assert "FOR UPDATE" in mock_db.fetch_one.await_args_list[2].args[0]
    assert mock_db.execute.await_count == 2
    assert "INSERT INTO kommo_message_jobs" in mock_db.execute.await_args_list[0].args[0]
    assert "INSERT INTO kommo_message_receipts" in mock_db.execute.await_args_list[1].args[0]

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
    )
    result = await jobs.record_incoming_event(event)
    assert result["status"] == "merged"
    update_call = mock_db.execute.await_args_list[0]
    assert update_call.args[1]["combined_message"] == "Hola\nTienen pijamas?"
    receipt_call = mock_db.execute.await_args_list[1]
    assert "INSERT INTO kommo_message_receipts" in receipt_call.args[0]
    assert receipt_call.args[1]["job_id"] == "pending-id"
    assert receipt_call.args[1]["receipt_status"] == "merged"
    assert all("'discarded'" not in call.args[0] for call in mock_db.execute.await_args_list)


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
    assert claim_values["salesbot_token_jti"] == "token-id"
    assert claim_values["entity_id"] == "100"
    assert "lead_id" not in claim_values
    assert "contact_id" not in claim_values
    fallback_values = mock_db.fetch_one.await_args_list[1].args[1]
    assert fallback_values == {"entity_type": "leads", "entity_id": "100"}


@pytest.mark.asyncio
async def test_salesbot_callback_uses_signed_lead_identity(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value={"id": "job", "status": "waiting_for_salesbot"})
    monkeypatch.setattr(jobs, "db", mock_db)

    result = await jobs.persist_salesbot_callback(
        SalesbotWidgetData(contact_id="200"),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        {"account_id": 123, "client_uid": "client-uuid", "entity_type": "leads", "entity_id": "100"},
    )
    query = mock_db.fetch_one.await_args.args[0]
    values = mock_db.fetch_one.await_args.args[1]
    assert result == {"status": "ready", "job_id": "job"}
    assert "candidate.lead_id = :entity_id" in query
    assert values["entity_type"] == "leads"
    assert values["entity_id"] == "100"
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
        {"account_id": 123, "client_uid": "client-uuid", "entity_type": "contacts", "entity_id": "200"},
    )
    query = mock_db.fetch_one.await_args.args[0]
    values = mock_db.fetch_one.await_args.args[1]
    assert result == {"status": "ready", "job_id": "job"}
    assert "candidate.contact_id = :entity_id" in query
    assert values["entity_type"] == "contacts"
    assert values["entity_id"] == "200"
    assert "lead_id" not in values
    assert "contact_id" not in values


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
        "callback_claims": values["callback_claims"],
        "salesbot_token_jti": "token-id",
        "salesbot_account_id": "123",
        "salesbot_user_id": "456",
        "salesbot_client_uuid": "client-uuid",
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
        "callback_claims",
        "salesbot_token_jti",
        "salesbot_account_id",
        "salesbot_user_id",
        "salesbot_client_uuid",
    }
    assert values["entity_type"] == "contacts"
    assert values["entity_id"] == "200"


@pytest.mark.asyncio
async def test_callback_without_waiting_job_uses_exact_fallback_bind_parameters(monkeypatch):
    from app.integrations.kommo import jobs

    strict_db = _StrictCallbackDB(update_job=None, latest_job=None)
    monkeypatch.setattr(jobs, "db", strict_db)

    result = await jobs.persist_salesbot_callback(
        SalesbotWidgetData(lead_id="100"),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        {
            "account_id": 123,
            "client_uid": "client-uuid",
            "entity_type": "leads",
            "entity_id": "100",
        },
    )

    assert result == {"status": "ignored", "reason": "no_waiting_job"}
    assert len(strict_db.calls) == 2
    assert strict_db.calls[1][1] == {"entity_type": "leads", "entity_id": "100"}


@pytest.mark.asyncio
async def test_duplicate_callback_uses_exact_fallback_bind_parameters(monkeypatch):
    from app.integrations.kommo import jobs

    strict_db = _StrictCallbackDB(update_job=None, latest_job={"id": "sent-job", "status": "sent"})
    monkeypatch.setattr(jobs, "db", strict_db)

    result = await jobs.persist_salesbot_callback(
        SalesbotWidgetData(lead_id="100"),
        "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        {
            "account_id": 123,
            "client_uid": "client-uuid",
            "entity_type": "leads",
            "entity_id": "100",
        },
    )

    assert result == {"status": "duplicate", "job_id": "sent-job"}
    assert strict_db.calls[1][1] == {"entity_type": "leads", "entity_id": "100"}


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
            {"account_id": 123, "client_uid": "client-uuid", "entity_type": "leads", "entity_id": "100"},
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
        lambda: SimpleNamespace(channel_backend="kommo", kommo_ai_active_enum_id=1),
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

    async def run_salesbot(_entity_id, _entity_type):
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
async def test_ready_job_discard_continues_salesbot_before_marking_discarded(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)

    client = MagicMock()
    client.continue_salesbot = AsyncMock(return_value={"accepted": True})
    job = {"id": "job", "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2"}

    await jobs._continue_and_discard_job(client, job, "global_ai_paused")

    client.continue_salesbot.assert_awaited_once()
    assert client.continue_salesbot.await_args.args == ("https://acme.kommo.com/api/v4/salesbot/1/continue/2",)
    assert client.continue_salesbot.await_args.kwargs == {
        "data": {"status": "fail", "message": ""},
    }
    assert mock_db.execute.await_count == 2
    assert "status = 'continuing'" in mock_db.execute.await_args_list[0].args[0]
    assert json.loads(mock_db.execute.await_args_list[0].args[1]["continuation_payload"]) == {
        "data": {"status": "fail", "message": ""},
    }
    assert "status = 'discarded'" in mock_db.execute.await_args_list[1].args[0]


@pytest.mark.asyncio
async def test_ready_job_sends_ai_reply_in_salesbot_data_message(monkeypatch):
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

    await jobs._process_ready_job({
        "id": "job",
        "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        "combined_message": "Catalogo",
        "channel": "whatsapp",
        "correlation_id": "corr",
    })

    client.continue_salesbot.assert_awaited_once()
    assert client.continue_salesbot.await_args.kwargs == {
        "data": {"status": "success", "message": reply},
    }
    continuation_payload = json.loads(mock_db.execute.await_args_list[0].args[1]["continuation_payload"])
    assert continuation_payload == {"data": {"status": "success", "message": reply}}
    assert "https://store.example/static/catalog/catalog.pdf" in continuation_payload["data"]["message"]
    assert "Aquí" in continuation_payload["data"]["message"]
    assert "💕" in continuation_payload["data"]["message"]
    assert "attachment_type" not in continuation_payload["data"]
    assert any("assistant_message_persisted_at" in call.args[0] for call in mock_db.execute.await_args_list)


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
    assert "status = 'sent'" in mock_db.execute.await_args_list[-1].args[0]


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
        "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        "combined_message": "Promo",
        "channel": "whatsapp",
        "correlation_id": "corr",
    })

    assert client.continue_salesbot.await_args.kwargs == {
        "data": {"status": "success", "message": "¡Hola! *Promo especial*"},
    }
    continuation_payload = json.loads(mock_db.execute.await_args_list[0].args[1]["continuation_payload"])
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
        "return_url": "https://acme.kommo.com/api/v4/salesbot/1/continue/2",
        "combined_message": "Catalogo",
        "channel": "whatsapp",
        "correlation_id": "corr",
    })

    assert client.continue_salesbot.await_args.kwargs == {
        "data": {"status": "success", "message": reply},
    }
    continuation_payload = json.loads(mock_db.execute.await_args_list[0].args[1]["continuation_payload"])
    assert continuation_payload == {"data": {"status": "success", "message": reply}}
    assert "attachment_type" not in continuation_payload["data"]


@pytest.mark.asyncio
async def test_accepted_continuation_log_does_not_claim_delivery(monkeypatch, caplog):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(jobs, "db", mock_db)

    with caplog.at_level("INFO", logger="app.integrations.kommo.jobs"):
        await jobs._mark_job_sent("job", {"accepted": True})

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

    await scheduler._check_scheduled_broadcasts()

    update_query = mock_db.execute.await_args.args[0]
    assert "status = 'failed'" in update_query
    assert "status = 'scheduled'" in update_query
