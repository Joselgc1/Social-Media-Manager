from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _DBHandle:
    def transaction(self):
        return _Tx()


def _customer(**overrides):
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    data = {
        "id": "customer-1",
        "conversation_state": "escalated",
        "escalation_source": "automatic",
        "escalated_at": now - timedelta(hours=4),
        "escalation_expires_at": now - timedelta(hours=1),
        "is_blocked": False,
    }
    data.update(overrides)
    return data


def _active_customer(**overrides):
    data = {
        "id": "customer-1",
        "conversation_state": "active",
        "escalation_source": None,
        "escalated_at": None,
        "escalation_expires_at": None,
        "is_blocked": False,
    }
    data.update(overrides)
    return data


def _install_escalation_db(monkeypatch, escalations, *, fetch_one_side_effect=None, fetch_all_return=None):
    mock_db = MagicMock()
    mock_db.get_db = MagicMock(return_value=_DBHandle())
    mock_db.get_settings = AsyncMock(return_value={})
    mock_db.fetch_one = AsyncMock(side_effect=fetch_one_side_effect) if fetch_one_side_effect is not None else AsyncMock(return_value=None)
    mock_db.fetch_all = AsyncMock(return_value=fetch_all_return or [])
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(escalations, "db", mock_db)
    return mock_db


@pytest.mark.asyncio
async def test_automatic_escalation_gets_three_hour_expiry_by_default(monkeypatch):
    from app.crm import escalations

    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    mock_db = _install_escalation_db(monkeypatch, escalations, fetch_one_side_effect=[_customer()])

    await escalations.escalate_customer_automatically("customer-1", settings={}, now=now)

    values = mock_db.fetch_one.await_args.args[1]
    assert values["expires_at"] == now + timedelta(minutes=180)


@pytest.mark.asyncio
async def test_configured_store_timeout_is_respected(monkeypatch):
    from app.crm import escalations

    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    mock_db = _install_escalation_db(monkeypatch, escalations, fetch_one_side_effect=[_customer()])

    await escalations.escalate_customer_automatically(
        "customer-1",
        settings={"automatic_escalation_timeout_minutes": 45},
        now=now,
    )

    values = mock_db.fetch_one.await_args.args[1]
    assert values["expires_at"] == now + timedelta(minutes=45)


@pytest.mark.asyncio
async def test_timeout_zero_creates_no_expiry(monkeypatch):
    from app.crm import escalations

    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    mock_db = _install_escalation_db(monkeypatch, escalations, fetch_one_side_effect=[_customer()])

    await escalations.escalate_customer_automatically(
        "customer-1",
        settings={"automatic_escalation_timeout_minutes": 0},
        now=now,
    )

    values = mock_db.fetch_one.await_args.args[1]
    assert values["expires_at"] is None


@pytest.mark.asyncio
async def test_manual_escalation_never_expires(monkeypatch):
    from app.crm import escalations

    mock_db = _install_escalation_db(monkeypatch, escalations, fetch_one_side_effect=[_customer(escalation_source="manual")])

    await escalations.escalate_customer_manually("customer-1")

    query = mock_db.fetch_one.await_args.args[0]
    assert "escalation_source = 'manual'" in query
    assert "escalation_expires_at = NULL" in query


@pytest.mark.asyncio
async def test_external_kommo_human_mode_gets_external_source_without_expiry(monkeypatch):
    from app.integrations.kommo import state

    mark_external = AsyncMock(return_value={"id": "customer-1", "escalation_source": "external", "escalation_expires_at": None})
    monkeypatch.setattr(state.escalations, "mark_external_escalation", mark_external)
    monkeypatch.setattr(
        state,
        "get_config",
        lambda: SimpleNamespace(kommo_ai_active_enum_id=1, kommo_ai_human_enum_id=2, kommo_ai_paused_enum_id=3),
    )

    await state.sync_local_state_from_ai_mode("customer-1", 2)

    mark_external.assert_awaited_once_with("customer-1")


@pytest.mark.asyncio
async def test_external_active_sync_does_not_clear_manual_or_automatic_escalations(monkeypatch):
    from app.crm import escalations

    mock_db = _install_escalation_db(monkeypatch, escalations, fetch_one_side_effect=[None])

    result = await escalations.mark_external_active("customer-1")

    query = mock_db.fetch_one.await_args.args[0]
    assert result is None
    assert "OR escalation_source = 'external'" in query


@pytest.mark.asyncio
async def test_reescalation_resets_automatic_expiry(monkeypatch):
    from app.crm import escalations

    now = datetime(2026, 1, 1, 15, 0, tzinfo=UTC)
    mock_db = _install_escalation_db(monkeypatch, escalations, fetch_one_side_effect=[_customer(escalated_at=now)])

    await escalations.escalate_customer_automatically(
        "customer-1",
        settings={"automatic_escalation_timeout_minutes": 30},
        now=now,
    )

    query = mock_db.fetch_one.await_args.args[0]
    values = mock_db.fetch_one.await_args.args[1]
    assert "OR escalation_source = 'automatic'" in query
    assert values["expires_at"] == now + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_expired_meta_customer_becomes_active_and_context_resets(monkeypatch):
    from app.crm import escalations

    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    expired = _customer(escalation_expires_at=now - timedelta(minutes=1))
    active = _active_customer()
    mock_db = _install_escalation_db(monkeypatch, escalations, fetch_one_side_effect=[expired, active])
    monkeypatch.setattr(escalations, "get_config", lambda: SimpleNamespace(channel_backend="meta"))
    monkeypatch.setattr(escalations.conversations, "clear_history", AsyncMock())
    monkeypatch.setattr(escalations.sessions, "reset_session", AsyncMock())

    result = await escalations.reactivate_if_expired(expired, now=now)

    assert result.status == "reactivated"
    assert result.provider == "meta"
    assert result.customer["conversation_state"] == "active"
    assert mock_db.fetch_one.await_count == 2
    escalations.conversations.clear_history.assert_awaited_once_with("customer-1")
    escalations.sessions.reset_session.assert_awaited_once_with("customer-1")


@pytest.mark.asyncio
async def test_expired_kommo_customer_changes_to_ai_active_and_is_verified(monkeypatch):
    from app.crm import escalations

    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    expired = _customer(escalation_expires_at=now - timedelta(minutes=1))
    active = _active_customer()
    _install_escalation_db(monkeypatch, escalations, fetch_one_side_effect=[expired, active])
    monkeypatch.setattr(
        escalations,
        "get_config",
        lambda: SimpleNamespace(channel_backend="kommo", kommo_ai_active_enum_id=222, kommo_ai_mode_field_id=111),
    )
    monkeypatch.setattr(escalations, "get_mapping_by_customer", AsyncMock(return_value={"external_lead_id": "100"}))
    monkeypatch.setattr(escalations.conversations, "clear_history", AsyncMock())
    monkeypatch.setattr(escalations.sessions, "reset_session", AsyncMock())
    client = MagicMock()
    client.update_ai_mode = AsyncMock()
    client.get_lead = AsyncMock(return_value={"custom_fields_values": [{"field_id": 111, "values": [{"enum_id": 222}]}]})
    monkeypatch.setattr(escalations.KommoClient, "from_config", lambda: client)

    result = await escalations.reactivate_if_expired(expired, now=now)

    assert result.status == "reactivated"
    assert result.provider == "kommo"
    assert result.kommo_lead_id == "100"
    client.update_ai_mode.assert_awaited_once_with("100", 222)
    client.get_lead.assert_awaited_once_with("100")


@pytest.mark.asyncio
async def test_kommo_failure_keeps_local_customer_escalated(monkeypatch):
    from app.crm import escalations

    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    expired = _customer(escalation_expires_at=now - timedelta(minutes=1))
    mock_db = _install_escalation_db(monkeypatch, escalations, fetch_one_side_effect=[expired])
    monkeypatch.setattr(
        escalations,
        "get_config",
        lambda: SimpleNamespace(channel_backend="kommo", kommo_ai_active_enum_id=222, kommo_ai_mode_field_id=111),
    )
    monkeypatch.setattr(escalations, "get_mapping_by_customer", AsyncMock(return_value={"external_lead_id": "100"}))
    monkeypatch.setattr(escalations.conversations, "clear_history", AsyncMock())
    monkeypatch.setattr(escalations.sessions, "reset_session", AsyncMock())
    client = MagicMock()
    client.update_ai_mode = AsyncMock(side_effect=RuntimeError("Bearer secret-token"))
    client.get_lead = AsyncMock()
    monkeypatch.setattr(escalations.KommoClient, "from_config", lambda: client)

    result = await escalations.reactivate_if_expired(expired, now=now)

    assert result.status == "failed"
    assert result.reason == "kommo_sync_failed"
    assert mock_db.fetch_one.await_count == 1
    escalations.conversations.clear_history.assert_not_awaited()
    escalations.sessions.reset_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocked_customers_are_never_reactivated(monkeypatch):
    from app.crm import escalations

    mock_db = _install_escalation_db(monkeypatch, escalations)
    blocked = _customer(conversation_state="blocked", is_blocked=True)

    result = await escalations.reactivate_if_expired(blocked, now=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))

    assert result.status == "skipped"
    assert result.reason == "blocked"
    mock_db.get_db.assert_not_called()


@pytest.mark.asyncio
async def test_manual_reactivation_clears_escalation_metadata_and_resets_context(monkeypatch):
    from app.crm import escalations

    active = _active_customer()
    mock_db = _install_escalation_db(monkeypatch, escalations, fetch_one_side_effect=[active])
    monkeypatch.setattr(escalations.conversations, "clear_history", AsyncMock())
    monkeypatch.setattr(escalations.sessions, "reset_session", AsyncMock())

    result = await escalations.mark_customer_active_for_admin("customer-1", channel="instagram")

    query = mock_db.fetch_one.await_args.args[0]
    values = mock_db.fetch_one.await_args.args[1]
    assert result["conversation_state"] == "active"
    assert values["channel"] == "instagram"
    assert "escalation_source = NULL" in query
    assert "escalated_at = NULL" in query
    assert "escalation_expires_at = NULL" in query
    escalations.conversations.clear_history.assert_awaited_once_with("customer-1")
    escalations.sessions.reset_session.assert_awaited_once_with("customer-1")


@pytest.mark.asyncio
async def test_manual_escalation_clears_previous_automatic_expiry(monkeypatch):
    from app.crm import escalations

    mock_db = _install_escalation_db(monkeypatch, escalations, fetch_one_side_effect=[_customer(escalation_source="manual")])

    await escalations.escalate_customer_manually("customer-1", channel="whatsapp")

    query = mock_db.fetch_one.await_args.args[0]
    values = mock_db.fetch_one.await_args.args[1]
    assert values["channel"] == "whatsapp"
    assert "escalation_source = 'manual'" in query
    assert "escalation_expires_at = NULL" in query


@pytest.mark.asyncio
async def test_scheduler_selects_only_expired_automatic_escalations(monkeypatch):
    from app.crm import escalations

    mock_db = _install_escalation_db(monkeypatch, escalations, fetch_all_return=[])

    result = await escalations.process_expired_automatic_escalations(now=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))

    query = mock_db.fetch_all.await_args.args[0]
    assert result["checked"] == 0
    assert "conversation_state = 'escalated'" in query
    assert "escalation_source = 'automatic'" in query
    assert "escalation_expires_at <= :now" in query
    assert "FOR UPDATE SKIP LOCKED" in query


@pytest.mark.asyncio
async def test_locked_processor_uses_conditional_update_to_prevent_stale_reactivation(monkeypatch):
    from app.crm import escalations

    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    expired = _customer(escalation_expires_at=now - timedelta(minutes=1))
    mock_db = _install_escalation_db(monkeypatch, escalations, fetch_one_side_effect=[expired, _active_customer()])
    monkeypatch.setattr(escalations, "get_config", lambda: SimpleNamespace(channel_backend="meta"))
    monkeypatch.setattr(escalations.conversations, "clear_history", AsyncMock())
    monkeypatch.setattr(escalations.sessions, "reset_session", AsyncMock())

    await escalations.reactivate_if_expired(expired, now=now)

    update_query = mock_db.fetch_one.await_args_list[1].args[0]
    assert "conversation_state = 'escalated'" in update_query
    assert "escalation_source = 'automatic'" in update_query
    assert "escalation_expires_at = :expected_expires_at" in update_query
    assert "escalated_at IS NOT DISTINCT FROM :expected_escalated_at" in update_query


@pytest.mark.asyncio
async def test_lazy_expiry_processing_allows_inbound_message_to_resume(monkeypatch):
    from app.ai import engine
    from app.ai.providers.base import LLMResponse

    settings = {
        "ai_enabled": True,
        "ai_orchestration_mode": "legacy",
        "llm_provider": "openai",
        "llm_model": "gpt-5.4-nano",
        "llm_temperature": 0.2,
        "llm_max_tokens": 500,
        "max_conversation_history": 20,
        "auto_fallback": False,
        "payment_methods": [],
    }
    expired_customer = _customer(id="customer-1", platform_id="58412", channel="whatsapp")
    active_customer = {**expired_customer, "conversation_state": "active", "escalation_source": None, "escalation_expires_at": None}
    provider = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse(text="Hola, seguimos por aquí.")))

    monkeypatch.setattr(engine.db, "get_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(engine.customers, "get_or_create_customer", AsyncMock(return_value=expired_customer))
    monkeypatch.setattr(engine.escalations, "reactivate_if_expired", AsyncMock(return_value=SimpleNamespace(status="reactivated", customer=active_customer)))
    monkeypatch.setattr(engine.conversations, "get_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(engine.conversations, "store_message", AsyncMock())
    monkeypatch.setattr(engine.orders, "get_latest_open_order", AsyncMock(return_value=None))
    monkeypatch.setattr(engine.sessions, "get_session", AsyncMock(return_value=None))
    monkeypatch.setattr(engine.analytics, "log_response", AsyncMock())
    monkeypatch.setattr(engine.analytics, "log_ai_run", AsyncMock())
    monkeypatch.setattr(engine, "get_config", lambda: SimpleNamespace(store_name="Tienda Rosa", ai_orchestration_mode="legacy"))
    monkeypatch.setattr(engine, "get_cached_catalog", lambda: [])
    monkeypatch.setattr(engine.agent_runner_module, "_list_providers", lambda: ["openai"])
    monkeypatch.setattr(engine.agent_runner_module, "get_provider", lambda name: provider)

    response = await engine.generate_response("whatsapp", "58412", "Hola")

    assert response["text"] == "Hola, seguimos por aquí."
    assert response["escalated"] is False
    provider.chat.assert_awaited_once()
