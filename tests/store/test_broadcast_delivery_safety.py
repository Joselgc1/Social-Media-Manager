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
        {"id": "delivery-1", "platform_id": "584121234567", "display_name": "Ana Pérez"},
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
    assert "FOR UPDATE SKIP LOCKED" in claim_query


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
