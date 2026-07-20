"""Dry-run tools for detecting malformed Kommo conversation histories."""

from __future__ import annotations

import argparse
import asyncio
import json

from app import db
from app.crm import conversations


async def repair_malformed_kommo_histories(*, clear_test_histories: bool = False, limit: int = 100) -> dict:
    affected = await find_malformed_kommo_histories(limit=limit)
    report = {
        "dry_run": not clear_test_histories,
        "affected_customer_count": len(affected),
        "cleared_customer_count": 0,
        "customers": affected,
    }
    if not clear_test_histories:
        return report

    test_customer_ids = [row["customer_id"] for row in affected if row["is_test_customer"]]
    await conversations.clear_history_for_customers(test_customer_ids)
    report["cleared_customer_count"] = len(test_customer_ids)
    return report


async def find_malformed_kommo_histories(*, limit: int = 100) -> list[dict]:
    rows = await db.fetch_all(
        """
        WITH ordered AS (
            SELECT
                conv.customer_id,
                conv.role,
                conv.created_at,
                LAG(conv.role) OVER (PARTITION BY conv.customer_id ORDER BY conv.created_at, conv.id) AS previous_role
            FROM conversations conv
            WHERE EXISTS (
                SELECT 1
                FROM customer_channel_mappings mapping
                WHERE mapping.customer_id = conv.customer_id
                  AND mapping.provider = 'kommo'
            )
        )
        SELECT
            c.id::text AS customer_id,
            c.channel,
            COUNT(*) FILTER (WHERE ordered.role = 'user') AS user_message_count,
            COUNT(*) FILTER (WHERE ordered.role = 'assistant') AS assistant_message_count,
            COUNT(*) FILTER (WHERE ordered.role = 'user' AND ordered.previous_role = 'user') AS consecutive_user_break_count,
            MIN(ordered.created_at) FILTER (WHERE ordered.role = 'user' AND ordered.previous_role = 'user') AS first_malformed_at,
            (
                COALESCE(c.platform_id, '') ILIKE 'test%%'
                OR COALESCE(c.display_name, '') ILIKE '%%test%%'
                OR EXISTS (
                    SELECT 1
                    FROM customer_channel_mappings mapping
                    WHERE mapping.customer_id = c.id
                      AND mapping.provider = 'kommo'
                      AND (
                          COALESCE(mapping.external_contact_id, '') ILIKE 'test%%'
                          OR COALESCE(mapping.external_lead_id, '') ILIKE 'test%%'
                          OR COALESCE(mapping.external_chat_id, '') ILIKE 'test%%'
                          OR COALESCE(mapping.external_talk_id, '') ILIKE 'test%%'
                      )
                )
            ) AS is_test_customer
        FROM ordered
        JOIN customers c ON c.id = ordered.customer_id
        GROUP BY c.id, c.channel, c.platform_id, c.display_name
        HAVING COUNT(*) FILTER (WHERE ordered.role = 'user' AND ordered.previous_role = 'user') > 0
        ORDER BY first_malformed_at DESC NULLS LAST
        LIMIT :limit
        """,
        {"limit": limit},
    )
    return [_row_to_report(row) for row in rows]


def _row_to_report(row) -> dict:
    return {
        "customer_id": str(_record_value(row, "customer_id")),
        "channel": _record_value(row, "channel"),
        "user_message_count": int(_record_value(row, "user_message_count", 0) or 0),
        "assistant_message_count": int(_record_value(row, "assistant_message_count", 0) or 0),
        "consecutive_user_break_count": int(_record_value(row, "consecutive_user_break_count", 0) or 0),
        "first_malformed_at": _timestamp_for_report(_record_value(row, "first_malformed_at")),
        "is_test_customer": bool(_record_value(row, "is_test_customer", False)),
    }


def _record_value(row, key: str, default=None):
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def _timestamp_for_report(value) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


async def _run(args) -> int:
    await db.connect()
    try:
        report = await repair_malformed_kommo_histories(
            clear_test_histories=args.clear_test_histories,
            limit=args.limit,
        )
    finally:
        await db.disconnect()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Report malformed Kommo conversation histories.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum affected customers to report.")
    parser.add_argument(
        "--clear-test-histories",
        action="store_true",
        help="Clear histories only for affected customers explicitly identified as test customers.",
    )
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
