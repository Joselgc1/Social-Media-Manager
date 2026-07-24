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
        external_author_id="author-id",
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
        "external_author_id": "author-id",
        "external_origin": "waba",
    }


@pytest.mark.asyncio
async def test_kommo_mapping_lookup_does_not_fall_back_to_lead_when_specific_identity_present(monkeypatch):
    from app.crm import channel_mappings

    lookups = []

    async def lookup_one(provider, column, value):
        lookups.append((provider, column, value))
        if column == "external_lead_id":
            return {"customer_id": "wrong-lead-customer"}
        return None

    monkeypatch.setattr(channel_mappings, "_lookup_one", lookup_one)

    result = await channel_mappings._lookup_existing_kommo_mapping({
        "lead_id": "lead-1",
        "contact_id": "contact-2",
        "chat_id": "chat-2",
    })

    assert result is None
    assert ("kommo", "external_lead_id", "lead-1") not in lookups


@pytest.mark.asyncio
async def test_kommo_mapping_lookup_uses_lead_only_without_specific_identity(monkeypatch):
    from app.crm import channel_mappings

    async def lookup_one(provider, column, value):
        if column == "external_lead_id" and value == "lead-1":
            return {"customer_id": "lead-customer"}
        return None

    monkeypatch.setattr(channel_mappings, "_lookup_one", lookup_one)

    result = await channel_mappings._lookup_existing_kommo_mapping({"lead_id": "lead-1"})

    assert result == {"customer_id": "lead-customer"}


@pytest.mark.asyncio
async def test_new_contact_on_existing_lead_inserts_its_own_mapping(monkeypatch):
    from app.crm import channel_mappings

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[None, {"id": "contact-2-mapping"}, {"id": "contact-2-mapping"}])
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(channel_mappings, "db", mock_db)

    result = await channel_mappings.upsert_mapping(
        customer_id="customer-2",
        provider="kommo",
        channel="whatsapp",
        external_contact_id="contact-2",
        external_lead_id="shared-lead",
    )

    assert result["id"] == "contact-2-mapping"
    insert_query = mock_db.fetch_one.await_args_list[1].args[0]
    assert "ON CONFLICT DO NOTHING" in insert_query
    assert mock_db.execute.await_count == 0
