from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest


def _contact(*, first_name=None, last_name=None, name=None, fields=None):
    return {
        "first_name": first_name,
        "last_name": last_name,
        "name": name,
        "custom_fields_values": fields or [],
    }


def _field(code, values, *, name=None):
    return {"field_code": code, "field_name": name, "values": values}


def test_lead_placeholders_and_ids_are_rejected_as_display_names():
    from app.integrations.kommo.customer_profile import normalize_display_name

    assert normalize_display_name("Lead #8424566") is None
    assert normalize_display_name("8424566") is None
    assert normalize_display_name("550e8400-e29b-41d4-a716-446655440000") is None
    assert normalize_display_name("Maria Perez") == "Maria Perez"


def test_kommo_contact_name_is_display_name_fallback():
    from app.integrations.kommo.customer_profile import build_kommo_customer_profile

    profile = build_kommo_customer_profile(
        job={"author_name": "Lead #8424566"},
        contact=_contact(first_name="Ana", last_name="Perez", name="Contacto Generico"),
    )

    assert profile.display_name == "Ana Perez"


def test_existing_real_display_name_is_not_overwritten_by_placeholder():
    from app.integrations.kommo.customer_profile import KommoCustomerProfile, compute_customer_profile_updates

    updates = compute_customer_profile_updates(
        {"display_name": "Ana Real", "phone": None, "instagram_handle": None},
        KommoCustomerProfile(display_name="Lead #8424566"),
    )

    assert "display_name" not in updates


def test_whatsapp_phone_is_extracted_and_normalized_from_contact_phone_field():
    from app.integrations.kommo.customer_profile import extract_contact_phone

    contact = _contact(fields=[_field("PHONE", [{"value": "+58 (412) 123-4567", "enum_code": "WORK"}])])

    assert extract_contact_phone(contact) == "+584121234567"


def test_mobile_phone_value_is_preferred():
    from app.integrations.kommo.customer_profile import extract_contact_phone

    contact = _contact(fields=[
        _field(
            "PHONE",
            [
                {"value": "+58 212 555 1212", "enum_code": "WORK"},
                {"value": "+58 412 123 4567", "enum_code": "MOB"},
            ],
        )
    ])

    assert extract_contact_phone(contact) == "+584121234567"


def test_chat_uuid_is_never_stored_as_phone_number():
    from app.integrations.kommo.customer_profile import KommoCustomerProfile, compute_customer_profile_updates

    chat_uuid = "550e8400-e29b-41d4-a716-446655440000"
    updates = compute_customer_profile_updates(
        {"display_name": None, "phone": chat_uuid, "instagram_handle": None},
        KommoCustomerProfile(),
        identifiers={chat_uuid},
    )

    assert updates["phone"] is None


def test_instagram_author_name_becomes_display_name_and_handle_is_normalized():
    from app.integrations.kommo.customer_profile import build_kommo_customer_profile

    contact = _contact(fields=[
        _field("INSTAGRAM", [{"value": "https://instagram.com/maria.bonita/?x=1#frag"}], name="Instagram")
    ])
    profile = build_kommo_customer_profile(job={"channel": "instagram", "author_name": "Maria Bonita"}, contact=contact)

    assert profile.display_name == "Maria Bonita"
    assert profile.instagram_handle == "maria.bonita"


def test_explicit_webhook_username_fields_are_preferred_for_instagram_handle():
    from app.integrations.kommo.customer_profile import build_kommo_customer_profile

    profile = build_kommo_customer_profile(
        job={
            "channel": "instagram",
            "author_username": "@maria.bonita_23",
            "sender_username": "weaker.sender",
        },
        contact=_contact(fields=[_field("INSTAGRAM", [{"value": "custom.field"}], name="Instagram")]),
    )

    assert profile.instagram_handle == "maria.bonita_23"
    assert profile.instagram_handle_source == "webhook_author_username"


def test_explicit_profile_urls_are_normalized_for_instagram_handle():
    from app.integrations.kommo.customer_profile import build_kommo_customer_profile

    profile = build_kommo_customer_profile(
        job={
            "channel": "instagram",
            "author_profile_url": "https://www.instagram.com/maria_bonita/?hl=es",
        },
    )

    assert profile.instagram_handle == "maria_bonita"
    assert profile.instagram_handle_source == "webhook_author_profile_url"


def test_contact_api_profile_fields_are_used_before_custom_fields():
    from app.integrations.kommo.customer_profile import build_kommo_customer_profile

    contact = _contact(
        name="Maria Bonita",
        fields=[_field("INSTAGRAM", [{"value": "custom.field"}], name="Instagram")],
    ) | {"profile_url": "https://instagram.com/contact.profile"}

    profile = build_kommo_customer_profile(job={"channel": "instagram"}, contact=contact)

    assert profile.instagram_handle == "contact.profile"
    assert profile.instagram_handle_source == "contact_api_profile_url"


def test_contact_api_username_fields_supply_instagram_handle():
    from app.integrations.kommo.customer_profile import build_kommo_customer_profile

    contact = _contact(name="Maria Bonita") | {"username": "@contact_user"}

    profile = build_kommo_customer_profile(job={"channel": "instagram"}, contact=contact)

    assert profile.instagram_handle == "contact_user"
    assert profile.instagram_handle_source == "contact_api_username"


def test_instagram_labelled_contact_custom_field_still_supplies_handle():
    from app.integrations.kommo.customer_profile import build_kommo_customer_profile

    contact = _contact(fields=[_field("TEXT", [{"value": "@custom_handle"}], name="Instagram")])

    profile = build_kommo_customer_profile(job={"channel": "instagram"}, contact=contact)

    assert profile.instagram_handle == "custom_handle"
    assert profile.instagram_handle_source == "contact_custom_field"


def test_handle_like_contact_name_is_lowest_priority_fallback():
    from app.integrations.kommo.customer_profile import build_kommo_customer_profile

    profile = build_kommo_customer_profile(
        job={"channel": "instagram"},
        contact=_contact(name="maria.bonita_23"),
    )

    assert profile.instagram_handle == "maria.bonita_23"
    assert profile.instagram_handle_source == "contact_name"


@pytest.mark.parametrize(
    "job,contact",
    [
        ({"channel": "instagram", "author_id": "maria.bonita"}, _contact(name="Maria Bonita")),
        ({"channel": "instagram", "contact_id": "420105"}, _contact(name="420105")),
        ({"channel": "instagram", "platform_id": "chat.handle"}, _contact(name="chat.handle")),
        ({"channel": "instagram", "chat_id": "550e8400-e29b-41d4-a716-446655440000"}, _contact(name="550e8400-e29b-41d4-a716-446655440000")),
        ({"channel": "instagram"}, _contact(name="Maria Bonita")),
        ({"channel": "instagram"}, _contact(name="cliente")),
    ],
)
def test_ids_uuids_generic_names_and_full_names_are_not_instagram_handles(job, contact):
    from app.integrations.kommo.customer_profile import build_kommo_customer_profile

    profile = build_kommo_customer_profile(job=job, contact=contact)

    assert profile.instagram_handle is None


def test_whatsapp_profile_behavior_ignores_new_explicit_instagram_sources():
    from app.integrations.kommo.customer_profile import build_kommo_customer_profile

    profile = build_kommo_customer_profile(
        job={
            "channel": "whatsapp",
            "author_username": "@maria.bonita",
            "author_profile_url": "https://instagram.com/maria.bonita",
        },
        contact=_contact(name="maria.bonita"),
    )

    assert profile.instagram_handle is None


def test_uuid_values_are_rejected_as_instagram_handles():
    from app.integrations.kommo.customer_profile import extract_instagram_handle, normalize_instagram_handle

    uuid = "550e8400-e29b-41d4-a716-446655440000"
    contact = _contact(fields=[_field("INSTAGRAM", [{"value": uuid}], name="Instagram")])

    assert normalize_instagram_handle(uuid) is None
    assert extract_instagram_handle(contact) is None


def test_existing_valid_instagram_handle_is_not_overwritten_by_weaker_data():
    from app.integrations.kommo.customer_profile import KommoCustomerProfile, compute_customer_profile_updates

    updates = compute_customer_profile_updates(
        {"display_name": None, "phone": None, "instagram_handle": "existing.handle"},
        KommoCustomerProfile(instagram_handle=None),
    )

    assert "instagram_handle" not in updates

    updates = compute_customer_profile_updates(
        {"display_name": None, "phone": None, "instagram_handle": "existing.handle"},
        KommoCustomerProfile(instagram_handle="custom_field"),
    )

    assert "instagram_handle" not in updates


@pytest.mark.asyncio
async def test_direct_meta_whatsapp_platform_id_may_still_be_phone(monkeypatch):
    from app.crm import customers

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[None, {"id": "customer", "phone": "584121234567"}])
    mock_db.execute = AsyncMock(return_value="customer")
    monkeypatch.setattr(customers, "db", mock_db)

    await customers.get_or_create_customer(channel="whatsapp", platform_id="584121234567")

    assert mock_db.execute.await_args.args[1]["phone"] == "584121234567"


@pytest.mark.asyncio
async def test_kommo_platform_id_is_not_copied_to_profile_fields(monkeypatch):
    from app.crm import customers

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[None, {"id": "customer", "phone": None, "instagram_handle": None}])
    mock_db.execute = AsyncMock(return_value="customer")
    monkeypatch.setattr(customers, "db", mock_db)

    await customers.get_or_create_customer(
        channel="whatsapp",
        platform_id="1234567890",
        allow_platform_phone_fallback=False,
    )

    values = mock_db.execute.await_args.args[1]
    assert values["phone"] is None
    assert values["instagram_handle"] is None


@pytest.mark.asyncio
async def test_existing_mapped_customer_is_enriched_without_duplication(monkeypatch):
    from app.crm import channel_mappings
    from app.integrations.kommo.customer_profile import KommoCustomerProfile

    customer = {
        "id": "customer-1",
        "channel": "whatsapp",
        "platform_id": "chat-uuid",
        "display_name": "Lead #8424566",
        "phone": None,
        "instagram_handle": None,
    }
    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value=customer)
    monkeypatch.setattr(channel_mappings, "db", mock_db)
    monkeypatch.setattr(channel_mappings, "_lookup_existing_kommo_mapping", AsyncMock(return_value={"customer_id": "customer-1"}))
    monkeypatch.setattr(channel_mappings.customers, "get_or_create_customer", AsyncMock())
    monkeypatch.setattr(channel_mappings, "upsert_mapping", AsyncMock(return_value={"id": "mapping"}))
    monkeypatch.setattr(
        channel_mappings,
        "enrich_customer_profile",
        AsyncMock(return_value=customer | {"display_name": "Maria Cliente"}),
    )

    result = await channel_mappings.resolve_customer_from_kommo_job(
        {"channel": "whatsapp", "chat_id": "chat-uuid", "author_id": "author-1"},
        profile=KommoCustomerProfile(display_name="Maria Cliente"),
    )

    channel_mappings.customers.get_or_create_customer.assert_not_awaited()
    channel_mappings.enrich_customer_profile.assert_awaited_once()
    assert result["display_name"] == "Maria Cliente"


@pytest.mark.asyncio
async def test_existing_instagram_customer_missing_handle_is_enriched_on_next_event(monkeypatch, caplog):
    from app.crm import channel_mappings
    from app.integrations.kommo import customer_profile

    existing = {
        "id": "customer-ig-1",
        "channel": "instagram",
        "platform_id": "chat-ig",
        "display_name": "Cliente Instagram",
        "phone": None,
        "instagram_handle": None,
    }
    refreshed = existing | {"instagram_handle": "maria.bonita"}
    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[{"customer_id": "customer-ig-1"}, existing, refreshed])
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(channel_mappings, "db", mock_db)
    monkeypatch.setattr(customer_profile, "db", mock_db)
    monkeypatch.setattr(channel_mappings, "upsert_mapping", AsyncMock(return_value={"id": "mapping"}))
    monkeypatch.setattr(channel_mappings.customers, "get_or_create_customer", AsyncMock())

    with caplog.at_level("INFO", logger="app.integrations.kommo.customer_profile"):
        result = await channel_mappings.resolve_customer_from_kommo_job(
            {
                "channel": "instagram",
                "contact_id": "200",
                "chat_id": "chat-ig",
                "author_username": "@maria.bonita",
                "author_name": "Cliente Instagram",
            }
        )

    channel_mappings.customers.get_or_create_customer.assert_not_awaited()
    values = mock_db.execute.await_args.args[1]
    assert values["instagram_handle"] == "maria.bonita"
    assert result["instagram_handle"] == "maria.bonita"
    assert "source=webhook_author_username" in caplog.text
    assert "maria.bonita" not in caplog.text


@pytest.mark.asyncio
async def test_new_kommo_identity_reuses_customer_with_same_instagram_handle(monkeypatch):
    from app.crm import channel_mappings
    from app.integrations.kommo.customer_profile import KommoCustomerProfile

    existing = {
        "id": "customer-ig-1",
        "channel": "instagram",
        "platform_id": "old-contact",
        "instagram_handle": "joselgc",
    }
    database = MagicMock()

    @asynccontextmanager
    async def transaction():
        yield

    database.transaction = transaction
    mock_db = MagicMock()
    mock_db.get_db = MagicMock(return_value=database)
    mock_db.fetch_one = AsyncMock(return_value={"locked": True})
    monkeypatch.setattr(channel_mappings, "db", mock_db)
    monkeypatch.setattr(
        channel_mappings,
        "_lookup_existing_kommo_mapping",
        AsyncMock(side_effect=[None, None]),
    )
    monkeypatch.setattr(
        channel_mappings,
        "_lookup_instagram_customer_by_handle",
        AsyncMock(return_value=existing),
    )
    monkeypatch.setattr(channel_mappings.customers, "get_or_create_customer", AsyncMock())
    upsert = AsyncMock(return_value={"id": "mapping"})
    enrich = AsyncMock(return_value=existing)
    monkeypatch.setattr(channel_mappings, "upsert_mapping", upsert)
    monkeypatch.setattr(channel_mappings, "enrich_customer_profile", enrich)

    result = await channel_mappings.resolve_customer_from_kommo_job(
        {
            "channel": "instagram",
            "contact_id": "new-contact",
            "chat_id": "new-chat",
            "author_id": "new-author",
        },
        profile=KommoCustomerProfile(
            instagram_handle="joselgc",
            instagram_handle_source="webhook_author_username",
        ),
    )

    channel_mappings.customers.get_or_create_customer.assert_not_awaited()
    assert result["id"] == "customer-ig-1"
    assert upsert.await_args.kwargs["customer_id"] == "customer-ig-1"
    assert upsert.await_args.kwargs["external_contact_id"] == "new-contact"
    assert "pg_advisory_xact_lock" in mock_db.fetch_one.await_args.args[0]


@pytest.mark.asyncio
async def test_instagram_handle_lookup_is_case_insensitive_and_deterministic(monkeypatch):
    from app.crm import channel_mappings

    fetch_one = AsyncMock(return_value={"id": "customer-1"})
    monkeypatch.setattr(channel_mappings.db, "fetch_one", fetch_one)

    assert await channel_mappings._lookup_instagram_customer_by_handle("JoseLGC") == {
        "id": "customer-1"
    }
    query, values = fetch_one.await_args.args
    assert "LOWER(instagram_handle) = LOWER(:instagram_handle)" in query
    assert "ORDER BY total_orders DESC, last_active DESC, first_contact ASC" in query
    assert values == {"instagram_handle": "JoseLGC"}


@pytest.mark.asyncio
async def test_new_kommo_customer_prefers_contact_id_and_preserves_metadata(monkeypatch):
    from app.crm import channel_mappings
    from app.integrations.kommo.customer_profile import KommoCustomerProfile

    customer = {"id": "customer-1", "channel": "whatsapp", "platform_id": "200"}
    monkeypatch.setattr(channel_mappings, "_lookup_existing_kommo_mapping", AsyncMock(return_value=None))
    monkeypatch.setattr(channel_mappings.customers, "get_or_create_customer", AsyncMock(return_value=customer))
    monkeypatch.setattr(channel_mappings, "upsert_mapping", AsyncMock(return_value={"id": "mapping"}))
    monkeypatch.setattr(channel_mappings, "enrich_customer_profile", AsyncMock(return_value=customer))

    await channel_mappings.resolve_customer_from_kommo_job(
        {
            "channel": "whatsapp",
            "lead_id": "100",
            "contact_id": "200",
            "chat_id": "chat-uuid",
            "talk_id": "talk-1",
            "author_id": "author-1",
            "origin": "waba",
        },
        profile=KommoCustomerProfile(display_name="Maria Cliente"),
    )

    create_kwargs = channel_mappings.customers.get_or_create_customer.await_args.kwargs
    assert create_kwargs["platform_id"] == "200"
    assert create_kwargs["allow_platform_phone_fallback"] is False
    mapping_kwargs = channel_mappings.upsert_mapping.await_args.kwargs
    assert mapping_kwargs["external_contact_id"] == "200"
    assert mapping_kwargs["external_lead_id"] == "100"
    assert mapping_kwargs["external_chat_id"] == "chat-uuid"
    assert mapping_kwargs["external_talk_id"] == "talk-1"
    assert mapping_kwargs["external_author_id"] == "author-1"
    assert mapping_kwargs["external_origin"] == "waba"


@pytest.mark.asyncio
async def test_existing_chat_id_platform_customer_resolves_through_mapping(monkeypatch):
    from app.crm import channel_mappings

    customer = {"id": "customer-1", "channel": "whatsapp", "platform_id": "chat-uuid"}
    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(return_value=customer)
    monkeypatch.setattr(channel_mappings, "db", mock_db)
    monkeypatch.setattr(channel_mappings, "_lookup_existing_kommo_mapping", AsyncMock(return_value={"customer_id": "customer-1"}))
    monkeypatch.setattr(channel_mappings.customers, "get_or_create_customer", AsyncMock())
    monkeypatch.setattr(channel_mappings, "upsert_mapping", AsyncMock(return_value={"id": "mapping"}))
    monkeypatch.setattr(channel_mappings, "enrich_customer_profile", AsyncMock(return_value=customer))

    result = await channel_mappings.resolve_customer_from_kommo_job({"channel": "whatsapp", "chat_id": "chat-uuid"})

    channel_mappings.customers.get_or_create_customer.assert_not_awaited()
    assert result["platform_id"] == "chat-uuid"


@pytest.mark.asyncio
async def test_backfill_dry_run_does_not_modify_data_or_mappings(monkeypatch):
    from app.integrations.kommo import customer_profile_backfill as backfill

    row = {
        "customer_id": "customer-1",
        "channel": "whatsapp",
        "platform_id": "chat-uuid",
        "display_name": "Lead #8424566",
        "phone": None,
        "instagram_handle": None,
        "external_contact_id": None,
        "external_lead_id": "100",
        "external_chat_id": "chat-uuid",
        "external_talk_id": "talk-1",
        "external_author_id": "author-1",
        "mapping_channel": "whatsapp",
        "author_name": "Maria Cliente",
        "author_id": "author-1",
        "external_origin": "waba",
    }
    mock_db = MagicMock()
    mock_db.fetch_all = AsyncMock(return_value=[row])
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(backfill, "db", mock_db)

    report = await backfill.backfill_kommo_customer_profiles(dry_run=True, limit=10)

    assert report == {"dry_run": True, "scanned": 1, "updated": 1, "skipped": 0, "failed": 0}
    mock_db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_uses_stored_sender_profile_url_for_instagram_handle(monkeypatch):
    from app.integrations.kommo import customer_profile_backfill as backfill

    row = {
        "customer_id": "customer-1",
        "channel": "instagram",
        "platform_id": "chat-uuid",
        "display_name": "Maria Cliente",
        "phone": None,
        "instagram_handle": None,
        "external_contact_id": None,
        "external_lead_id": "100",
        "external_chat_id": "chat-uuid",
        "external_talk_id": "talk-1",
        "external_author_id": "author-1",
        "mapping_channel": "instagram",
        "author_name": "Maria Cliente",
        "author_id": "author-1",
        "author_username": None,
        "author_profile_url": None,
        "sender_username": None,
        "sender_profile_url": "https://www.instagram.com/maria.backfill/?hl=es",
        "external_origin": "instagram",
    }
    mock_db = MagicMock()
    mock_db.fetch_all = AsyncMock(return_value=[row])
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(backfill, "db", mock_db)

    captured = {}

    def capture_updates(customer, profile, *, identifiers=None):
        captured["profile"] = profile
        return {"instagram_handle": profile.instagram_handle} if profile.instagram_handle else {}

    monkeypatch.setattr(backfill, "compute_customer_profile_updates", capture_updates)

    report = await backfill.backfill_kommo_customer_profiles(dry_run=True, limit=10)

    assert report == {"dry_run": True, "scanned": 1, "updated": 1, "skipped": 0, "failed": 0}
    assert captured["profile"].instagram_handle == "maria.backfill"
    assert captured["profile"].instagram_handle_source == "webhook_sender_profile_url"
    mock_db.execute.assert_not_awaited()
