from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _database():
    database = MagicMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    database.transaction.return_value = transaction
    return database


def _broadcast(**overrides):
    values = {
        "id": "broadcast-1",
        "name": "Promo VIP",
        "template_name": "promo_vip",
        "template_params": ["Hola {first_name}"],
        "target_tags": ["vip"],
        "target_channel": "whatsapp",
        "status": "draft",
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_customer_query_requires_explicit_whatsapp_marketing_consent(monkeypatch):
    from app.broadcast import sender

    fetch_all = AsyncMock(return_value=[])
    monkeypatch.setattr(sender.db, "fetch_all", fetch_all)

    await sender._query_customers_by_tags(["vip"], "whatsapp")

    query, values = fetch_all.await_args.args
    assert "channel = 'whatsapp'" in query
    assert "marketing_opt_in = TRUE" in query
    assert "marketing_opt_in_at IS NOT NULL" in query
    assert values == {"tags_json": '["vip"]'}


@pytest.mark.asyncio
async def test_instagram_broadcast_is_rejected_before_campaign_claim(monkeypatch):
    from app.broadcast import sender

    fetch_one = AsyncMock(return_value=_broadcast(target_channel="instagram"))
    monkeypatch.setattr(sender, "db", SimpleNamespace(fetch_one=fetch_one))
    monkeypatch.setattr(sender, "get_config", lambda: SimpleNamespace(channel_backend="meta"))

    result = await sender.execute_broadcast("broadcast-1")

    assert "only" in result["error"]
    assert fetch_one.await_count == 1


@pytest.mark.asyncio
async def test_concurrent_sender_cannot_claim_same_campaign(monkeypatch):
    from app.broadcast import sender

    fetch_one = AsyncMock(side_effect=[_broadcast(), None])
    monkeypatch.setattr(sender, "db", SimpleNamespace(fetch_one=fetch_one))
    monkeypatch.setattr(sender, "get_config", lambda: SimpleNamespace(channel_backend="meta"))

    result = await sender.execute_broadcast("broadcast-1")

    assert "already claimed" in result["error"]
    claim_query = fetch_one.await_args_list[1].args[0]
    assert "status IN ('draft', 'scheduled')" in claim_query
    assert "RETURNING *" in claim_query


@pytest.mark.asyncio
async def test_broadcast_records_and_sends_each_claimed_recipient_once(monkeypatch):
    from app.broadcast import sender
    from app.channels import whatsapp_sender

    broadcast = _broadcast(status="sending")
    fetch_one = AsyncMock(side_effect=[
        _broadcast(),
        broadcast,
        {"audience_seeded_at": None},
        {"id": "delivery-1", "platform_id": "584121234567", "display_name": "Ana Pérez", "status": "sending"},
        None,
        {"sent": 1, "failed": 0, "sending": 0, "pending": 0},
    ])
    execute = AsyncMock(return_value=None)
    mock_db = SimpleNamespace(fetch_one=fetch_one, execute=execute, get_db=lambda: _database())
    send_template = AsyncMock(return_value={"message_id": "wamid-1"})
    monkeypatch.setattr(sender, "db", mock_db)
    monkeypatch.setattr(sender, "get_config", lambda: SimpleNamespace(channel_backend="meta"))
    monkeypatch.setattr(sender, "notify_owner", AsyncMock(return_value=None))
    monkeypatch.setattr(sender.asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(whatsapp_sender, "send_template", send_template)

    result = await sender.execute_broadcast("broadcast-1")

    assert result == {
        "broadcast_id": "broadcast-1",
        "recipients": 1,
        "errors": 0,
        "status": "sent",
        "pending_reconciliation": 0,
    }
    send_template.assert_awaited_once_with(
        to="584121234567",
        template_name="promo_vip",
        language="es",
        parameters=["Hola Ana"],
    )
    queries = [call.args[0] for call in execute.await_args_list]
    assert any("INSERT INTO broadcast_deliveries" in query for query in queries)
    assert any("status = 'sent'" in query for query in queries)
    claim_query = fetch_one.await_args_list[3].args[0]
    assert "status = 'pending'" in claim_query
    assert "FOR UPDATE OF d SKIP LOCKED" in claim_query
    assert "marketing_opt_in = TRUE" in claim_query
    assert "conversation_state != 'blocked'" in claim_query


@pytest.mark.asyncio
async def test_claim_skips_recipient_that_opted_out_after_audience_seed(monkeypatch):
    from app.broadcast import sender

    fetch_one = AsyncMock(side_effect=[
        {"id": "delivery-1", "platform_id": "584121234567", "display_name": "Ana", "status": "failed"},
        None,
    ])
    monkeypatch.setattr(sender.db, "fetch_one", fetch_one)

    claimed = await sender._claim_next_delivery("broadcast-1")

    assert claimed is None
    claim_query = fetch_one.await_args_list[0].args[0]
    assert "recipient_ineligible_at_claim" in claim_query
    assert "marketing_opt_in = TRUE" in claim_query
    assert "is_blocked = FALSE" in claim_query


@pytest.mark.asyncio
async def test_seeded_audience_is_not_rebuilt_after_reset(monkeypatch):
    from app.broadcast import sender

    execute = AsyncMock()
    mock_db = SimpleNamespace(
        fetch_one=AsyncMock(return_value={"audience_seeded_at": "2026-07-22T10:00:00Z"}),
        execute=execute,
        get_db=lambda: _database(),
    )
    monkeypatch.setattr(sender, "db", mock_db)

    await sender._seed_delivery_ledger("broadcast-1", ["vip"])

    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_marketing_consent_update_records_opt_in_timestamp(monkeypatch):
    from app.crm import customers

    fetch_one = AsyncMock(side_effect=[{"id": "customer-1"}, {"id": "customer-1", "marketing_opt_in": True}])
    execute = AsyncMock()
    monkeypatch.setattr(customers.db, "fetch_one", fetch_one)
    monkeypatch.setattr(customers.db, "execute", execute)

    result = await customers.update_customer("customer-1", marketing_opt_in=True)

    query, values = execute.await_args.args
    assert "marketing_opt_in_at" in query
    assert "marketing_opt_out_at" in query
    assert values["marketing_opt_in"] is True
    assert result["marketing_opt_in"] is True


@pytest.mark.asyncio
async def test_broadcast_reset_preserves_recipient_delivery_ledger(monkeypatch):
    from app import db
    from app.broadcast import api

    fetch_one = AsyncMock(return_value={"id": "broadcast-1", "status": "failed"})
    execute = AsyncMock()
    monkeypatch.setattr(db, "fetch_one", fetch_one)
    monkeypatch.setattr(db, "execute", execute)

    result = await api.reset_broadcast_endpoint("broadcast-1")

    assert result["status"] == "draft"
    query = execute.await_args.args[0]
    assert "UPDATE broadcasts" in query
    assert "status = 'sent'" in query


@pytest.mark.asyncio
async def test_delivery_ledger_is_available_for_reconciliation(monkeypatch):
    from app.broadcast import sender

    rows = [{
        "id": "delivery-1",
        "platform_id": "584121234567",
        "status": "sending",
        "attempt_count": 1,
    }]
    fetch_all = AsyncMock(return_value=rows)
    monkeypatch.setattr(sender.db, "fetch_all", fetch_all)

    result = await sender.list_broadcast_deliveries("broadcast-1", limit=5000)

    assert result == rows
    query, values = fetch_all.await_args.args
    assert "FROM broadcast_deliveries" in query
    assert values == {"broadcast_id": "broadcast-1", "limit": 1000}


@pytest.mark.asyncio
async def test_stale_broadcast_claims_requeue_only_before_the_meta_send(monkeypatch):
    from app.broadcast import sender

    execute = AsyncMock(side_effect=[2, 1])
    monkeypatch.setattr(sender.db, "execute", execute)

    result = await sender.recover_stale_broadcast_deliveries("broadcast-1")

    assert result == {"requeued": 2, "delivery_unknown": 1}
    before_send_query = execute.await_args_list[0].args[0]
    attempted_query = execute.await_args_list[1].args[0]
    assert "outbound_started_at IS NULL" in before_send_query
    assert "status = 'pending'" in before_send_query
    assert "CAST(:broadcast_id AS uuid) IS NULL" in before_send_query
    assert "outbound_started_at IS NOT NULL" in attempted_query
    assert "status = 'delivery_unknown'" in attempted_query
    assert "CAST(:broadcast_id AS uuid) IS NULL" in attempted_query


@pytest.mark.asyncio
async def test_meta_delivery_unknown_is_quarantined_for_manual_reconciliation(monkeypatch):
    from app.broadcast import sender
    from app.channels import whatsapp_sender
    from app.channels.meta_errors import MetaSendError

    fetch_one = AsyncMock(side_effect=[
        _broadcast(),
        _broadcast(status="sending"),
        {"audience_seeded_at": None},
        {"id": "delivery-1", "platform_id": "584121234567", "display_name": "Ana", "status": "sending"},
        None,
        {"sent": 0, "failed": 0, "sending": 0, "pending": 0},
    ])
    execute = AsyncMock()
    mock_db = SimpleNamespace(fetch_one=fetch_one, execute=execute, get_db=lambda: _database())
    monkeypatch.setattr(sender, "db", mock_db)
    monkeypatch.setattr(sender, "get_config", lambda: SimpleNamespace(channel_backend="meta"))
    monkeypatch.setattr(sender, "notify_owner", AsyncMock())
    monkeypatch.setattr(
        whatsapp_sender,
        "send_template",
        AsyncMock(side_effect=MetaSendError("5xx", retryable=False, delivery_known=False)),
    )

    await sender.execute_broadcast("broadcast-1")

    update_call = next(call for call in execute.await_args_list if "SET status = :status" in call.args[0])
    assert update_call.args[1]["status"] == "delivery_unknown"
    assert update_call.args[1]["error"] == "delivery_unknown_meta_send"


@pytest.mark.asyncio
async def test_reconciled_ambiguous_delivery_can_be_explicitly_retried(monkeypatch):
    from app import db
    from app.broadcast import api

    fetch_one = AsyncMock(return_value={"id": "delivery-1"})
    monkeypatch.setattr(db, "fetch_one", fetch_one)

    result = await api.retry_reconciled_delivery_endpoint(
        "broadcast-1", "delivery-1", api.DeliveryRetryConfirmation(confirmed_not_delivered=True)
    )

    assert result == {"delivery_id": "delivery-1", "status": "pending"}
    query, values = fetch_one.await_args.args
    assert "status = 'delivery_unknown'" in query
    assert values == {"delivery_id": "delivery-1", "broadcast_id": "broadcast-1"}




def test_broadcast_meta_message_id_extracts_graph_api_response():
    from app.broadcast.sender import _meta_message_id

    assert _meta_message_id({"messages": [{"id": "wamid.1"}]}) == "wamid.1"
