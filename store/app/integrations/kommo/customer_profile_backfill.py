"""Manual backfill for Kommo customer profile enrichment."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any

from app import db
from app.integrations.kommo.client import KommoAPIError, KommoClient, sanitize_kommo_error
from app.integrations.kommo.customer_profile import (
    build_kommo_customer_profile,
    compute_customer_profile_updates,
    enrich_customer_profile,
    kommo_identifier_values,
)

logger = logging.getLogger(__name__)


async def backfill_kommo_customer_profiles(*, dry_run: bool = True, limit: int = 200) -> dict[str, int | bool]:
    rows = await _candidate_rows(limit=limit)
    report: dict[str, int | bool] = {
        "dry_run": dry_run,
        "scanned": len(rows),
        "updated": 0,
        "skipped": 0,
        "failed": 0,
    }
    client = KommoClient.from_config() if any(_value(row, "external_contact_id") for row in rows) else None

    for row in rows:
        customer = _customer_from_row(row)
        job_like = _job_from_row(row)
        contact = await _safe_get_contact(client, _value(row, "external_contact_id"), customer_id=str(customer["id"]))
        profile = build_kommo_customer_profile(job=job_like, contact=contact)
        identifiers = kommo_identifier_values(job_like, row)
        updates = compute_customer_profile_updates(customer, profile, identifiers=identifiers)
        if not updates:
            report["skipped"] = int(report["skipped"]) + 1
            continue

        if not dry_run:
            try:
                await enrich_customer_profile(customer, profile, identifiers=identifiers)
            except Exception as e:
                report["failed"] = int(report["failed"]) + 1
                logger.warning(
                    "Kommo profile backfill customer update failed: customer_id=%s error=%s",
                    customer["id"],
                    sanitize_kommo_error(e),
                )
                continue

        report["updated"] = int(report["updated"]) + 1

    return report


async def _candidate_rows(*, limit: int) -> list[Any]:
    return await db.fetch_all(
        r"""
        SELECT DISTINCT ON (customer.id)
            customer.id::text AS customer_id,
            customer.channel,
            customer.platform_id,
            customer.display_name,
            customer.phone,
            customer.instagram_handle,
            mapping.external_contact_id,
            mapping.external_lead_id,
            mapping.external_chat_id,
            mapping.external_talk_id,
            mapping.external_author_id,
            mapping.external_origin,
            mapping.channel AS mapping_channel,
            latest_job.author_name,
            COALESCE(latest_job.author_id, latest_receipt.author_id, mapping.external_author_id) AS author_id
        FROM customer_channel_mappings mapping
        JOIN customers customer ON customer.id = mapping.customer_id
        LEFT JOIN LATERAL (
            SELECT job.author_name, job.author_id
            FROM kommo_message_jobs job
            WHERE (
                (mapping.external_contact_id IS NOT NULL AND job.contact_id = mapping.external_contact_id)
                OR (mapping.external_lead_id IS NOT NULL AND job.lead_id = mapping.external_lead_id)
                OR (mapping.external_chat_id IS NOT NULL AND job.chat_id = mapping.external_chat_id)
                OR (mapping.external_talk_id IS NOT NULL AND job.talk_id = mapping.external_talk_id)
                OR (mapping.external_author_id IS NOT NULL AND job.author_id = mapping.external_author_id)
            )
            ORDER BY job.updated_at DESC NULLS LAST, job.created_at DESC
            LIMIT 1
        ) latest_job ON TRUE
        LEFT JOIN LATERAL (
            SELECT receipt.author_id
            FROM kommo_message_receipts receipt
            WHERE (
                (mapping.external_contact_id IS NOT NULL AND receipt.contact_id = mapping.external_contact_id)
                OR (mapping.external_lead_id IS NOT NULL AND receipt.lead_id = mapping.external_lead_id)
                OR (mapping.external_chat_id IS NOT NULL AND receipt.chat_id = mapping.external_chat_id)
                OR (mapping.external_talk_id IS NOT NULL AND receipt.talk_id = mapping.external_talk_id)
                OR (mapping.external_author_id IS NOT NULL AND receipt.author_id = mapping.external_author_id)
            )
            ORDER BY receipt.created_at DESC
            LIMIT 1
        ) latest_receipt ON TRUE
        WHERE mapping.provider = 'kommo'
          AND (
              customer.display_name IS NULL
              OR customer.display_name ~* '^lead\s+#\d+$'
              OR customer.display_name ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
              OR customer.display_name ~* '^\d+$'
              OR (customer.channel = 'whatsapp' AND customer.phone IS NULL)
              OR customer.phone ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
              OR (customer.channel = 'instagram' AND customer.instagram_handle IS NULL)
              OR customer.instagram_handle ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
          )
        ORDER BY customer.id, mapping.updated_at DESC
        LIMIT :limit
        """,
        {"limit": limit},
    )


async def _safe_get_contact(client: KommoClient | None, contact_id: str | None, *, customer_id: str) -> dict | None:
    if not client or not contact_id:
        return None
    try:
        return await client.get_contact(contact_id)
    except KommoAPIError as e:
        logger.warning(
            "Kommo profile backfill contact fetch skipped: customer_id=%s contact_id=%s error=%s",
            customer_id,
            contact_id,
            sanitize_kommo_error(e),
        )
    except Exception as e:
        logger.warning(
            "Kommo profile backfill contact fetch skipped: customer_id=%s contact_id=%s error=%s",
            customer_id,
            contact_id,
            sanitize_kommo_error(e),
        )
    return None


def _customer_from_row(row) -> dict[str, Any]:
    return {
        "id": _value(row, "customer_id"),
        "channel": _value(row, "channel"),
        "platform_id": _value(row, "platform_id"),
        "display_name": _value(row, "display_name"),
        "phone": _value(row, "phone"),
        "instagram_handle": _value(row, "instagram_handle"),
    }


def _job_from_row(row) -> dict[str, Any]:
    return {
        "channel": _value(row, "mapping_channel") or _value(row, "channel"),
        "lead_id": _value(row, "external_lead_id"),
        "contact_id": _value(row, "external_contact_id"),
        "chat_id": _value(row, "external_chat_id"),
        "talk_id": _value(row, "external_talk_id"),
        "author_id": _value(row, "author_id"),
        "author_name": _value(row, "author_name"),
        "origin": _value(row, "external_origin"),
    }


def _value(row, key: str, default=None):
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


async def _run(args) -> int:
    await db.connect()
    try:
        report = await backfill_kommo_customer_profiles(dry_run=not args.apply, limit=args.limit)
    finally:
        await db.disconnect()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Kommo customer profile fields from safe profile sources.")
    parser.add_argument("--limit", type=int, default=200, help="Maximum Kommo-mapped customers to scan.")
    parser.add_argument("--dry-run", action="store_true", help="Preview counts without writing changes. This is the default.")
    parser.add_argument("--apply", action="store_true", help="Apply profile updates. Without this flag, no data is modified.")
    args = parser.parse_args()
    if args.dry_run and args.apply:
        parser.error("Use either --dry-run or --apply, not both.")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
