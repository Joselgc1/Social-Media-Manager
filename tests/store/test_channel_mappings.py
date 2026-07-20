from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_upsert_mapping_existing_row_filters_update_bind_values(monkeypatch):
    from app.crm import channel_mappings

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(
        side_effect=[
            {"id": "mapping-id"},
            {"id": "mapping-id", "customer_id": "customer-id"},
        ]
    )
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(channel_mappings, "db", mock_db)

    result = await channel_mappings.upsert_mapping(
        customer_id="customer-id",
        provider="kommo",
        channel="whatsapp",
        external_contact_id="contact-id",
        external_lead_id="lead-id",
        external_chat_id="chat-id",
        external_talk_id="talk-id",
        external_origin="waba",
    )

    update_values = mock_db.execute.await_args.args[1]
    assert result["id"] == "mapping-id"
    assert "provider" not in update_values
    assert "channel" not in update_values
    assert update_values == {
        "id": "mapping-id",
        "customer_id": "customer-id",
        "external_contact_id": "contact-id",
        "external_lead_id": "lead-id",
        "external_chat_id": "chat-id",
        "external_talk_id": "talk-id",
        "external_origin": "waba",
    }
