"""Read-only Instagram customer continuity audit with privacy-safe output."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable
from typing import Any

import asyncpg

AUDIT_QUERY = """
WITH instagram_kommo_customers AS (
    SELECT DISTINCT mapping.customer_id
    FROM customer_channel_mappings mapping
    WHERE mapping.provider = 'kommo'
      AND mapping.channel = 'instagram'
),
meta_igsid_mappings AS (
    SELECT mapping.id, mapping.customer_id
    FROM customer_channel_mappings mapping
    WHERE mapping.provider = 'meta'
      AND mapping.channel = 'instagram'
      AND mapping.external_author_id IS NOT NULL
      AND BTRIM(mapping.external_author_id) <> ''
),
kommo_history_without_meta AS (
    SELECT DISTINCT mapping.customer_id
    FROM customer_channel_mappings mapping
    WHERE mapping.provider = 'kommo'
      AND mapping.channel = 'instagram'
      AND EXISTS (
          SELECT 1
          FROM conversations conversation
          WHERE conversation.customer_id = mapping.customer_id
            AND conversation.channel = 'instagram'
      )
      AND NOT EXISTS (
          SELECT 1
          FROM meta_igsid_mappings meta_mapping
          WHERE meta_mapping.customer_id = mapping.customer_id
      )
),
shared_identifier_duplicates AS (
    SELECT DISTINCT
        kommo_mapping.customer_id AS kommo_customer_id,
        meta_mapping.customer_id AS meta_customer_id
    FROM customer_channel_mappings kommo_mapping
    JOIN customer_channel_mappings meta_mapping
      ON meta_mapping.provider = 'meta'
     AND meta_mapping.channel = 'instagram'
     AND meta_mapping.external_author_id = kommo_mapping.external_author_id
    WHERE kommo_mapping.provider = 'kommo'
      AND kommo_mapping.channel = 'instagram'
      AND kommo_mapping.external_author_id IS NOT NULL
      AND BTRIM(kommo_mapping.external_author_id) <> ''
      AND kommo_mapping.customer_id <> meta_mapping.customer_id
)
SELECT
    (SELECT COUNT(*) FROM instagram_kommo_customers) AS instagram_kommo_count,
    (SELECT COUNT(*) FROM meta_igsid_mappings) AS meta_igsid_count,
    (SELECT COUNT(*) FROM kommo_history_without_meta) AS history_without_meta_count,
    (SELECT COUNT(*) FROM shared_identifier_duplicates) AS duplicate_count
"""


async def build_report(connection: asyncpg.Connection) -> dict[str, Any]:
    """Run the continuity audit in one read-only, time-bounded snapshot."""
    async with connection.transaction(readonly=True):
        await connection.execute("SET LOCAL statement_timeout = '5000ms'")
        row = await connection.fetchrow(AUDIT_QUERY)

    return {
        "read_only": True,
        "instagram_customers_with_kommo_mappings": int(row["instagram_kommo_count"]),
        "meta_igsid_mappings": int(row["meta_igsid_count"]),
        "kommo_instagram_history_without_meta_mapping": int(row["history_without_meta_count"]),
        "shared_identifier_duplicate_candidates": int(row["duplicate_count"]),
    }


async def run_audit(
    database_url: str,
    *,
    connector: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Connect only to PostgreSQL and return the sanitized audit report."""
    if not database_url:
        raise ValueError("DATABASE_URL is required")
    connect = connector or asyncpg.connect
    connection = await connect(database_url, command_timeout=6)
    try:
        return await build_report(connection)
    finally:
        await connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit Instagram customer continuity without changing data or exposing identifiers."
    )
    return parser


def main() -> int:
    _parser().parse_args()
    try:
        report = asyncio.run(
            run_audit(
                os.environ.get("DATABASE_URL", "").strip(),
            )
        )
    except Exception:
        print("Instagram continuity audit failed; inspect secure application logs.", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
