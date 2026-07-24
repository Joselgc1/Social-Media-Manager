"""Provider-neutral customer/channel identifier mappings."""

from __future__ import annotations

import json
from typing import Any

from app import db
from app.crm import customers
from app.integrations.kommo.customer_profile import (
    build_kommo_customer_profile,
    enrich_customer_profile,
    kommo_identifier_values,
)


async def get_mapping_by_customer(customer_id: str, provider: str = "kommo") -> dict | None:
    row = await db.fetch_one(
        """
        SELECT * FROM customer_channel_mappings
        WHERE customer_id = :customer_id AND provider = :provider
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        {"customer_id": customer_id, "provider": provider},
    )
    return dict(row) if row else None


async def lookup_by_provider(provider: str, channel: str, external_id: str) -> dict | None:
    for column in ("external_contact_id", "external_lead_id", "external_chat_id", "external_talk_id", "external_author_id"):
        row = await db.fetch_one(
            f"""
            SELECT * FROM customer_channel_mappings
            WHERE provider = :provider
              AND channel = :channel
              AND {column} = :external_id
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            {"provider": provider, "channel": channel, "external_id": external_id},
        )
        if row:
            return dict(row)
    return None


async def lookup_by_external_contact_id(provider: str, external_contact_id: str) -> dict | None:
    return await _lookup_one(provider, "external_contact_id", external_contact_id)


async def lookup_by_lead_id(provider: str, external_lead_id: str) -> dict | None:
    return await _lookup_one(provider, "external_lead_id", external_lead_id)


async def lookup_by_chat_id(provider: str, external_chat_id: str) -> dict | None:
    return await _lookup_one(provider, "external_chat_id", external_chat_id)


async def lookup_by_talk_id(provider: str, external_talk_id: str) -> dict | None:
    return await _lookup_one(provider, "external_talk_id", external_talk_id)


async def lookup_by_author_id(provider: str, external_author_id: str) -> dict | None:
    return await _lookup_one(provider, "external_author_id", external_author_id)


async def upsert_mapping(
    *,
    customer_id: str,
    provider: str,
    channel: str,
    external_contact_id: str | None = None,
    external_lead_id: str | None = None,
    external_chat_id: str | None = None,
    external_talk_id: str | None = None,
    external_author_id: str | None = None,
    external_origin: str | None = None,
) -> dict:
    values = {
        "customer_id": customer_id,
        "provider": provider,
        "channel": channel,
        "external_contact_id": external_contact_id,
        "external_lead_id": external_lead_id,
        "external_chat_id": external_chat_id,
        "external_talk_id": external_talk_id,
        "external_author_id": external_author_id,
        "external_origin": external_origin,
    }
    row = await _find_mapping_for_upsert(values)
    if row:
        await db.execute(
            """
            UPDATE customer_channel_mappings
            SET customer_id = :customer_id,
                external_contact_id = COALESCE(:external_contact_id, external_contact_id),
                external_lead_id = COALESCE(:external_lead_id, external_lead_id),
                external_chat_id = COALESCE(:external_chat_id, external_chat_id),
                external_talk_id = COALESCE(:external_talk_id, external_talk_id),
                external_author_id = COALESCE(:external_author_id, external_author_id),
                external_origin = COALESCE(:external_origin, external_origin),
                updated_at = NOW()
            WHERE id = :id
            """,
            _mapping_update_values(values, row["id"]),
        )
        mapping = await db.fetch_one("SELECT * FROM customer_channel_mappings WHERE id = :id", {"id": row["id"]})
        return dict(mapping)

    inserted = await db.fetch_one(
        """
        INSERT INTO customer_channel_mappings (
            customer_id, provider, channel, external_contact_id, external_lead_id,
            external_chat_id, external_talk_id, external_author_id, external_origin
        ) VALUES (
            :customer_id, :provider, :channel, :external_contact_id, :external_lead_id,
            :external_chat_id, :external_talk_id, :external_author_id, :external_origin
        )
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        values,
    )
    if not inserted:
        row = await _find_mapping_for_upsert(values)
        if not row:
            raise RuntimeError("Could not upsert customer channel mapping")
        await db.execute(
            """
            UPDATE customer_channel_mappings
            SET customer_id = :customer_id,
                external_contact_id = COALESCE(:external_contact_id, external_contact_id),
                external_lead_id = COALESCE(:external_lead_id, external_lead_id),
                external_chat_id = COALESCE(:external_chat_id, external_chat_id),
                external_talk_id = COALESCE(:external_talk_id, external_talk_id),
                external_author_id = COALESCE(:external_author_id, external_author_id),
                external_origin = COALESCE(:external_origin, external_origin),
                updated_at = NOW()
            WHERE id = :id
            """,
            _mapping_update_values(values, row["id"]),
        )
        mapping = await db.fetch_one("SELECT * FROM customer_channel_mappings WHERE id = :id", {"id": row["id"]})
        return dict(mapping)

    mapping_id = inserted["id"]
    mapping = await db.fetch_one("SELECT * FROM customer_channel_mappings WHERE id = :id", {"id": mapping_id})
    return dict(mapping)


async def resolve_customer_from_kommo_job(
    job: dict,
    lead: dict | None = None,
    contact: dict | None = None,
    profile=None,
) -> dict:
    channel = job.get("channel") or "whatsapp"
    platform_id = _local_platform_id(job)
    profile = profile or build_kommo_customer_profile(job=job, contact=contact)
    identifiers = kommo_identifier_values(job)
    _ = lead
    mapping = await _lookup_existing_kommo_mapping(job)
    if mapping:
        customer = await db.fetch_one("SELECT * FROM customers WHERE id = :id", {"id": mapping["customer_id"]})
        if customer:
            await upsert_mapping(
                customer_id=str(customer["id"]),
                provider="kommo",
                channel=channel,
                external_contact_id=job.get("contact_id"),
                external_lead_id=job.get("lead_id"),
                external_chat_id=job.get("chat_id"),
                external_talk_id=job.get("talk_id"),
                external_author_id=job.get("author_id"),
                external_origin=job.get("origin"),
            )
            return await enrich_customer_profile(dict(customer), profile, identifiers=identifiers)

    customer = await customers.get_or_create_customer(
        channel=channel,
        platform_id=platform_id,
        allow_platform_phone_fallback=False,
    )
    await upsert_mapping(
        customer_id=customer["id"],
        provider="kommo",
        channel=channel,
        external_contact_id=job.get("contact_id"),
        external_lead_id=job.get("lead_id"),
        external_chat_id=job.get("chat_id"),
        external_talk_id=job.get("talk_id"),
        external_author_id=job.get("author_id"),
        external_origin=job.get("origin"),
    )
    return await enrich_customer_profile(customer, profile, identifiers=identifiers)


async def resolve_customer_from_kommo_event(event, lead: dict | None = None) -> dict:
    job_like = {
        "channel": event.channel or "whatsapp",
        "lead_id": event.lead_id,
        "contact_id": event.contact_id,
        "chat_id": event.chat_id,
        "talk_id": event.talk_id,
        "author_id": event.author_id,
        "author_name": event.author_name,
        "author_username": event.author_username,
        "author_profile_url": event.author_profile_url,
        "sender_username": event.sender_username,
        "sender_profile_url": event.sender_profile_url,
        "origin": event.origin,
    }
    return await resolve_customer_from_kommo_job(job_like, lead=lead)


async def _lookup_existing_kommo_mapping(job: dict) -> dict | None:
    for key, value in (
        ("external_contact_id", job.get("contact_id")),
        ("external_lead_id", job.get("lead_id")),
        ("external_chat_id", job.get("chat_id")),
        ("external_talk_id", job.get("talk_id")),
        ("external_author_id", job.get("author_id")),
    ):
        if not value:
            continue
        mapping = await _lookup_one("kommo", key, str(value))
        if mapping:
            return mapping
    return None


async def _lookup_one(provider: str, column: str, value: str) -> dict | None:
    if column not in {"external_contact_id", "external_lead_id", "external_chat_id", "external_talk_id", "external_author_id"}:
        raise ValueError("Invalid mapping lookup column")
    row = await db.fetch_one(
        f"""
        SELECT * FROM customer_channel_mappings
        WHERE provider = :provider AND {column} = :value
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        {"provider": provider, "value": value},
    )
    return dict(row) if row else None


async def _find_mapping_for_upsert(values: dict[str, Any]) -> dict | None:
    lookup_order = (
        ("external_contact_id", values.get("external_contact_id")),
        ("external_lead_id", values.get("external_lead_id")),
        ("external_chat_id", values.get("external_chat_id")),
        ("external_talk_id", values.get("external_talk_id")),
    )
    if not any(value for _, value in lookup_order):
        lookup_order += (("external_author_id", values.get("external_author_id")),)

    for column, value in lookup_order:
        if not value:
            continue
        row = await db.fetch_one(
            f"""
            SELECT id FROM customer_channel_mappings
            WHERE provider = :provider
              AND channel = :channel
              AND {column} = :value
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            {"provider": values["provider"], "channel": values["channel"], "value": value},
        )
        if row:
            return dict(row)
    return None


def _local_platform_id(job: dict[str, Any]) -> str:
    return str(job.get("contact_id") or job.get("chat_id") or job.get("talk_id") or job.get("lead_id") or "kommo_unknown")


def _mapping_update_values(values: dict[str, Any], mapping_id: str) -> dict[str, Any]:
    return {
        "id": mapping_id,
        "customer_id": values["customer_id"],
        "external_contact_id": values.get("external_contact_id"),
        "external_lead_id": values.get("external_lead_id"),
        "external_chat_id": values.get("external_chat_id"),
        "external_talk_id": values.get("external_talk_id"),
        "external_author_id": values.get("external_author_id"),
        "external_origin": values.get("external_origin"),
    }


def _json(value):
    if isinstance(value, str):
        return json.loads(value)
    return value
