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
    assert mock_db.execute.await_count == 1

    mock_db.fetch_one = AsyncMock(side_effect=[None, {"id": "existing", "status": "pending"}])
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

    monkeypatch.setattr(jobs, "_find_waiting_job_for_callback", AsyncMock(return_value={"id": "job", "status": "sent"}))
    result = await jobs.persist_salesbot_callback(SalesbotWidgetData(lead_id="100"), "https://acme.kommo.com/api/v4/salesbot/1/continue/2")
    assert result == {"status": "duplicate", "job_id": "job"}


@pytest.mark.asyncio
async def test_stale_job_recovery_runs_all_updates(monkeypatch):
    from app.integrations.kommo import jobs

    mock_db = MagicMock()
    mock_db.execute = AsyncMock(return_value=1)
    monkeypatch.setattr(jobs, "db", mock_db)
    result = await jobs.recover_stale_jobs()
    assert mock_db.execute.await_count == 3
    assert result["failed_waiting"] == 1


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
