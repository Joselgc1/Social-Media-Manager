from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException


def _kommo_config(**overrides):
    data = {
        "channel_backend": "kommo",
        "kommo_ai_mode_field_id": 111,
        "kommo_ai_active_enum_id": 222,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _lead_with_ai_mode(enum_id: int) -> dict:
    return {
        "custom_fields_values": [
            {
                "field_id": 111,
                "values": [{"enum_id": enum_id}],
            }
        ]
    }


@pytest.mark.asyncio
async def test_admin_activation_syncs_kommo_before_local_update_and_history_clear(monkeypatch):
    from app.admin import customer_activation

    events = []
    monkeypatch.setattr(customer_activation, "get_config", lambda: _kommo_config())
    monkeypatch.setattr(
        customer_activation,
        "get_mapping_by_customer",
        AsyncMock(return_value={"external_lead_id": "100"}),
    )

    client = MagicMock()

    async def update_ai_mode(lead_id, enum_id):
        events.append(("kommo_update", lead_id, enum_id))

    async def get_lead(lead_id):
        events.append(("kommo_get", lead_id))
        return _lead_with_ai_mode(222)

    client.update_ai_mode = AsyncMock(side_effect=update_ai_mode)
    client.get_lead = AsyncMock(side_effect=get_lead)
    monkeypatch.setattr(customer_activation.KommoClient, "from_config", lambda: client)

    async def update_customer(*, customer_id, channel=None, conversation_state=None):
        events.append(("local_update", customer_id, channel, conversation_state))
        return {"id": customer_id, "channel": channel, "conversation_state": conversation_state}

    async def clear_history(customer_id):
        events.append(("clear_history", customer_id))

    monkeypatch.setattr(customer_activation.customer_crm, "update_customer", AsyncMock(side_effect=update_customer))
    monkeypatch.setattr(customer_activation.conversations, "clear_history", AsyncMock(side_effect=clear_history))

    result = await customer_activation.activate_customer_for_admin({"id": "customer"}, channel="instagram")

    assert result.status == "activated"
    assert result.kommo_lead_id == "100"
    assert events == [
        ("kommo_update", "100", 222),
        ("kommo_get", "100"),
        ("local_update", "customer", "instagram", "active"),
        ("clear_history", "customer"),
    ]


@pytest.mark.asyncio
async def test_admin_activation_failure_does_not_update_local_state_or_clear_history(monkeypatch):
    from app.admin import customer_activation

    monkeypatch.setattr(customer_activation, "get_config", lambda: _kommo_config())
    monkeypatch.setattr(
        customer_activation,
        "get_mapping_by_customer",
        AsyncMock(return_value={"external_lead_id": "100"}),
    )

    client = MagicMock()
    client.update_ai_mode = AsyncMock(side_effect=RuntimeError("Bearer secret-token"))
    client.get_lead = AsyncMock()
    monkeypatch.setattr(customer_activation.KommoClient, "from_config", lambda: client)

    update_customer = AsyncMock()
    clear_history = AsyncMock()
    monkeypatch.setattr(customer_activation.customer_crm, "update_customer", update_customer)
    monkeypatch.setattr(customer_activation.conversations, "clear_history", clear_history)

    with pytest.raises(customer_activation.ManualActivationError) as exc:
        await customer_activation.activate_customer_for_admin({"id": "customer"})

    assert "secret-token" not in exc.value.safe_detail
    assert "redacted" in exc.value.safe_detail.lower()
    update_customer.assert_not_awaited()
    clear_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_activation_without_kommo_mapping_is_local_only(monkeypatch):
    from app.admin import customer_activation

    monkeypatch.setattr(customer_activation, "get_config", lambda: _kommo_config())
    monkeypatch.setattr(customer_activation, "get_mapping_by_customer", AsyncMock(return_value=None))
    from_config = MagicMock(side_effect=AssertionError("Kommo client should not be created"))
    monkeypatch.setattr(customer_activation.KommoClient, "from_config", from_config)

    monkeypatch.setattr(
        customer_activation.customer_crm,
        "update_customer",
        AsyncMock(return_value={"id": "customer", "conversation_state": "active"}),
    )
    monkeypatch.setattr(customer_activation.conversations, "clear_history", AsyncMock())

    result = await customer_activation.activate_customer_for_admin({"id": "customer"})

    assert result.status == "local_only"
    assert result.kommo_lead_id is None
    from_config.assert_not_called()
    customer_activation.customer_crm.update_customer.assert_awaited_once()
    customer_activation.conversations.clear_history.assert_awaited_once_with("customer")


@pytest.mark.asyncio
async def test_admin_activation_in_meta_backend_is_local_only_without_mapping_lookup(monkeypatch):
    from app.admin import customer_activation

    monkeypatch.setattr(customer_activation, "get_config", lambda: _kommo_config(channel_backend="meta"))
    get_mapping = AsyncMock()
    monkeypatch.setattr(customer_activation, "get_mapping_by_customer", get_mapping)
    monkeypatch.setattr(
        customer_activation.customer_crm,
        "update_customer",
        AsyncMock(return_value={"id": "customer", "conversation_state": "active"}),
    )
    monkeypatch.setattr(customer_activation.conversations, "clear_history", AsyncMock())

    result = await customer_activation.activate_customer_for_admin({"id": "customer"})

    assert result.status == "local_only"
    get_mapping.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_customer_returns_502_for_kommo_activation_failure(monkeypatch):
    from app.admin import settings

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(
        return_value={
            "id": "customer",
            "display_name": "Cliente",
            "channel": "whatsapp",
            "platform_id": "58412",
            "conversation_state": "escalated",
        }
    )
    monkeypatch.setattr(settings, "db", mock_db)
    monkeypatch.setattr(
        settings,
        "activate_customer_for_admin",
        AsyncMock(
            side_effect=settings.ManualActivationError(
                "Kommo activation failed: timeout",
                customer_id="customer",
                kommo_lead_id="100",
            )
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await settings.resolve_customer("customer")

    assert exc.value.status_code == 502
    assert exc.value.detail == "Kommo activation failed: timeout"


@pytest.mark.asyncio
async def test_update_customer_to_active_uses_activation_helper(monkeypatch):
    from app.admin import settings

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(
        return_value={
            "id": "customer",
            "channel": "whatsapp",
            "platform_id": "58412",
            "conversation_state": "escalated",
        }
    )
    monkeypatch.setattr(settings, "db", mock_db)
    activation = AsyncMock(
        return_value=SimpleNamespace(
            customer={"id": "customer", "conversation_state": "active"},
            status="activated",
        )
    )
    monkeypatch.setattr(settings, "activate_customer_for_admin", activation)
    monkeypatch.setattr(settings.customer_crm, "update_customer", AsyncMock())

    response = await settings.update_customer("customer", settings.CustomerUpdate(conversation_state="active"))

    assert response == {
        "status": "updated",
        "customer": {"id": "customer", "conversation_state": "active"},
        "activation_status": "activated",
    }
    activation.assert_awaited_once()
    settings.customer_crm.update_customer.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_all_customers_returns_per_customer_partial_results(monkeypatch):
    from app.admin import settings

    rows = [
        {"id": "activated", "channel": "whatsapp", "platform_id": "1", "conversation_state": "escalated"},
        {"id": "local", "channel": "instagram", "platform_id": "2", "conversation_state": "escalated"},
        {"id": "failed", "channel": "whatsapp", "platform_id": "3", "conversation_state": "escalated"},
    ]
    mock_db = MagicMock()
    mock_db.fetch_all = AsyncMock(return_value=rows)
    monkeypatch.setattr(settings, "db", mock_db)

    async def activate_customer(row):
        if row["id"] == "failed":
            raise settings.ManualActivationError("Kommo activation failed: timeout", customer_id="failed")
        status = "activated" if row["id"] == "activated" else "local_only"
        return SimpleNamespace(customer_id=row["id"], status=status)

    monkeypatch.setattr(settings, "activate_customer_for_admin", AsyncMock(side_effect=activate_customer))

    response = await settings.resolve_all_customers()

    assert response == {
        "status": "partial",
        "resolved": 2,
        "failed": 1,
        "results": [
            {"customer_id": "activated", "status": "activated"},
            {"customer_id": "local", "status": "local_only"},
            {"customer_id": "failed", "status": "failed", "detail": "Kommo activation failed: timeout"},
        ],
    }
