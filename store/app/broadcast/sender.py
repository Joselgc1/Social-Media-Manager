"""
Broadcast sender.
Queries customers by tag filters, sends WhatsApp template messages
to each matching customer, and logs results.

WhatsApp broadcast rules:
- Only works on WhatsApp (Instagram has no outbound broadcast API)
- Must use pre-approved template messages (submitted via Meta Business Manager)
- Customers must have opted in to receive marketing messages
- Cost: ~$0.0851 per marketing message for Venezuela
- Rate limit: starts at 250 unique contacts/24h, scales after business verification
"""

import asyncio
import json
import logging
from datetime import datetime

from app import db
from app.admin.notify import notify_owner
from app.channels.meta_errors import MetaSendError
from app.config import get_config
from app.customer_identity import extract_safe_first_name

logger = logging.getLogger(__name__)

# Pause between sends to respect rate limits (seconds)
SEND_DELAY = 0.5


def _mask_platform_id(value: str) -> str:
    if len(value) <= 4:
        return value
    return f"{value[:2]}***{value[-2:]}"


async def execute_broadcast(broadcast_id: str) -> dict:
    """
    Execute a broadcast: query matching customers, send templates, update status.

    Returns a summary dict with recipient count and any errors.
    """
    if not getattr(get_config(), "outbound_processing_enabled", True):
        return {"error": "Outbound processing is disabled."}

    # Load the broadcast record
    broadcast = await db.fetch_one(
        "SELECT * FROM broadcasts WHERE id = :id",
        {"id": broadcast_id},
    )

    if not broadcast:
        return {"error": f"Broadcast {broadcast_id} not found."}

    if broadcast["status"] not in ("draft", "scheduled"):
        return {"error": f"Broadcast already in status '{broadcast['status']}'. Cannot re-send."}

    if get_config().channel_backend == "kommo":
        return {
            "error": (
                "WhatsApp broadcast delivery is unavailable while CHANNEL_BACKEND=kommo. "
                "Use Kommo broadcasts or an approved Kommo WhatsApp template flow."
            )
        }

    channel = str(broadcast["target_channel"] or "whatsapp").strip().lower()
    if channel != "whatsapp":
        return {"error": "Broadcast delivery is supported only for opted-in WhatsApp customers."}

    from app.channels.whatsapp_sender import send_template

    broadcast = await db.fetch_one(
        """
        UPDATE broadcasts
        SET status = 'sending', sent_at = NOW()
        WHERE id = :id AND status IN ('draft', 'scheduled')
        RETURNING *
        """,
        {"id": broadcast_id},
    )
    if not broadcast:
        return {"error": "Broadcast was already claimed by another sender."}

    try:
        target_tags = broadcast["target_tags"]
        if isinstance(target_tags, str):
            target_tags = json.loads(target_tags)

        template_name = broadcast["template_name"]
        template_params = broadcast["template_params"]
        if isinstance(template_params, str):
            template_params = json.loads(template_params)

        await _seed_delivery_ledger(broadcast_id, target_tags)

        while True:
            delivery = await _claim_next_delivery(broadcast_id)
            if not delivery:
                break
            try:
                customer = {
                    "display_name": delivery["display_name"],
                    "platform_id": delivery["platform_id"],
                }
                params = _personalize_params(template_params, customer)
                await db.execute(
                    """
                    UPDATE broadcast_deliveries
                    SET outbound_started_at = NOW(), updated_at = NOW()
                    WHERE id = :id AND status = 'sending'
                    """,
                    {"id": delivery["id"]},
                )
                response = await send_template(
                    to=delivery["platform_id"],
                    template_name=template_name,
                    language="es",
                    parameters=params,
                )
                await db.execute(
                    """
                    UPDATE broadcast_deliveries
                    SET status = 'sent', sent_at = NOW(),
                        meta_message_id = :meta_message_id, updated_at = NOW()
                    WHERE id = :id AND status = 'sending'
                    """,
                    {"id": delivery["id"], "meta_message_id": _meta_message_id(response)},
                )
                await asyncio.sleep(SEND_DELAY)
            except Exception as e:
                logger.error(
                    "Broadcast send failed for %s: %s",
                    _mask_platform_id(delivery["platform_id"]),
                    e,
                )
                known_safe_failure = isinstance(e, MetaSendError) and e.delivery_known
                delivery_unknown = isinstance(e, MetaSendError) and not e.delivery_known
                await db.execute(
                    """
                    UPDATE broadcast_deliveries
                    SET status = :status, failed_at = NOW(), last_error = :error, updated_at = NOW()
                    WHERE id = :id AND status = 'sending'
                    """,
                    {
                        "id": delivery["id"],
                        "status": "delivery_unknown" if delivery_unknown else "failed",
                        "error": "known_meta_send_failure" if known_safe_failure else (
                            "delivery_unknown_meta_send" if delivery_unknown else type(e).__name__[:100]
                        ),
                    },
                )

        counts = await _delivery_counts(broadcast_id)
        sent = counts["sent"]
        errors = counts["failed"] + counts["sending"] + counts["delivery_unknown"]
        final_status = "sent" if errors == 0 else ("partial" if sent > 0 else "failed")
        await db.execute(
            "UPDATE broadcasts SET status = :status, recipients = :sent WHERE id = :id",
            {"status": final_status, "sent": sent, "id": broadcast_id},
        )

        # Notify admin
        await notify_owner(
            f"📢 *Broadcast completado*\n\n"
            f"*Nombre:* {broadcast['name']}\n"
            f"*Enviados:* {sent}\n"
            f"*Errores:* {errors}\n"
            f"*Plantilla:* {template_name}"
        )

        logger.info(f"Broadcast {broadcast_id} complete: {sent} sent, {errors} errors.")

        return {
            "broadcast_id": broadcast_id,
            "recipients": sent,
            "errors": errors,
            "status": final_status,
            "pending_reconciliation": counts["sending"],
        }

    except Exception as e:
        logger.error(f"Broadcast {broadcast_id} crashed: {e}")
        await db.execute(
            "UPDATE broadcasts SET status = 'failed' WHERE id = :id",
            {"id": broadcast_id},
        )
        raise


async def _query_customers_by_tags(tags: list[str], channel: str = "whatsapp") -> list[dict]:
    """
    Find customers that match ALL specified tags on the given channel.

    Uses PostgreSQL's JSONB @> operator for efficient tag matching.
    Each tag must be present in the customer's tags array.
    """
    if not tags or channel != "whatsapp":
        return []

    # Build a single JSONB containment check: tags must contain ALL specified tags
    tags_json = json.dumps(tags)

    query = """
        SELECT id, platform_id, display_name, tags
        FROM customers
        WHERE channel = 'whatsapp'
          AND marketing_opt_in = TRUE
          AND marketing_opt_in_at IS NOT NULL
          AND is_blocked = FALSE
          AND conversation_state != 'blocked'
          AND CAST(tags AS jsonb) @> CAST(:tags_json AS jsonb)
        ORDER BY last_active DESC
    """

    rows = await db.fetch_all(query, {"tags_json": tags_json})
    return [dict(r) for r in rows]


async def _seed_delivery_ledger(broadcast_id: str, tags: list[str]) -> None:
    """Snapshot the eligible audience once so resets cannot expand or duplicate it."""
    async with db.get_db().transaction():
        broadcast = await db.fetch_one(
            "SELECT audience_seeded_at FROM broadcasts WHERE id = :id FOR UPDATE",
            {"id": broadcast_id},
        )


        if not broadcast or broadcast["audience_seeded_at"] is not None:
            return

        if tags:
            await db.execute(
                """
            INSERT INTO broadcast_deliveries (
                broadcast_id, customer_id, platform_id, channel, display_name
            )
            SELECT :broadcast_id, id, platform_id, 'whatsapp', display_name
            FROM customers
            WHERE channel = 'whatsapp'
              AND marketing_opt_in = TRUE
              AND marketing_opt_in_at IS NOT NULL
              AND is_blocked = FALSE
              AND conversation_state != 'blocked'
              AND CAST(tags AS jsonb) @> CAST(:tags_json AS jsonb)
            ON CONFLICT (broadcast_id, channel, platform_id) DO NOTHING
            """,
                {"broadcast_id": broadcast_id, "tags_json": json.dumps(tags)},
            )
        await db.execute(
            "UPDATE broadcasts SET audience_seeded_at = NOW() WHERE id = :id",
            {"id": broadcast_id},
        )


async def recover_stale_broadcast_deliveries(broadcast_id: str | None = None) -> dict[str, int]:
    """Recover definitely unsent claims and quarantine ambiguous Meta attempts."""
    requeued = await db.execute(
        """
        UPDATE broadcast_deliveries
        SET status = 'pending', claimed_at = NULL, updated_at = NOW(),
            last_error = 'recovered_before_meta_send'
        WHERE (:broadcast_id IS NULL OR broadcast_id = :broadcast_id)
          AND status = 'sending'
          AND outbound_started_at IS NULL
          AND claimed_at < NOW() - INTERVAL '5 minutes'
        """,
        {"broadcast_id": broadcast_id},
    )
    unknown = await db.execute(
        """
        UPDATE broadcast_deliveries
        SET status = 'delivery_unknown', failed_at = NOW(), updated_at = NOW(),
            last_error = 'delivery_unknown_after_stale_meta_attempt'
        WHERE (:broadcast_id IS NULL OR broadcast_id = :broadcast_id)
          AND status = 'sending'
          AND outbound_started_at IS NOT NULL
          AND claimed_at < NOW() - INTERVAL '5 minutes'
        """,
        {"broadcast_id": broadcast_id},
    )
    return {"requeued": int(requeued or 0), "delivery_unknown": int(unknown or 0)}


async def _claim_next_delivery(broadcast_id: str):
    """Atomically claim one never-attempted recipient across all workers."""
    while True:
        row = await db.fetch_one(
            """
            WITH candidate AS (
                SELECT d.id,
                       (
                           c.id IS NOT NULL
                           AND c.channel = 'whatsapp'
                           AND c.marketing_opt_in = TRUE
                           AND c.marketing_opt_in_at IS NOT NULL
                           AND c.is_blocked = FALSE
                           AND c.conversation_state != 'blocked'
                       ) AS eligible
                FROM broadcast_deliveries d
                LEFT JOIN customers c ON c.id = d.customer_id
                WHERE d.broadcast_id = :broadcast_id AND d.status = 'pending'
                ORDER BY d.created_at, d.id
                FOR UPDATE OF d SKIP LOCKED
                LIMIT 1
            )
            UPDATE broadcast_deliveries AS delivery
            SET status = CASE WHEN candidate.eligible THEN 'sending' ELSE 'failed' END,
                attempt_count = CASE WHEN candidate.eligible THEN attempt_count + 1 ELSE attempt_count END,
                claimed_at = CASE WHEN candidate.eligible THEN NOW() ELSE claimed_at END,
                failed_at = CASE WHEN candidate.eligible THEN failed_at ELSE NOW() END,
                last_error = CASE WHEN candidate.eligible THEN last_error ELSE 'recipient_ineligible_at_claim' END,
                updated_at = NOW()
            FROM candidate
            WHERE delivery.id = candidate.id
            RETURNING delivery.id, delivery.platform_id, delivery.display_name, delivery.status
            """,
            {"broadcast_id": broadcast_id},
        )
        if not row:
            return None
        if row["status"] == "sending":
            return row
        logger.info("Skipped broadcast delivery %s because recipient is no longer eligible", row["id"])


async def _delivery_counts(broadcast_id: str) -> dict[str, int]:
    row = await db.fetch_one(
        """
        SELECT
            COUNT(*) FILTER (WHERE status = 'sent') AS sent,
            COUNT(*) FILTER (WHERE status = 'failed') AS failed,
            COUNT(*) FILTER (WHERE status = 'sending') AS sending,
            COUNT(*) FILTER (WHERE status = 'pending') AS pending
            , COUNT(*) FILTER (WHERE status = 'delivery_unknown') AS delivery_unknown
        FROM broadcast_deliveries
        WHERE broadcast_id = :broadcast_id
        """,
        {"broadcast_id": broadcast_id},
    )
    return {
        key: int(row[key] or 0) if row and key in row else 0
        for key in ("sent", "failed", "sending", "pending", "delivery_unknown")
    }


def _meta_message_id(response: dict | None) -> str | None:
    response = response or {}
    for message in response.get("messages") or []:
        if message_id := str((message or {}).get("id") or "").strip():
            return message_id
    return str(response.get("message_id") or response.get("id") or "").strip() or None


def _personalize_params(template_params: list | dict | None, customer: dict) -> list[str] | None:
    """
    Replace placeholders in template parameters with customer-specific values.

    Supports placeholders: {name}, {first_name}
    """
    if not template_params:
        return None

    params = template_params if isinstance(template_params, list) else []
    name = customer.get("display_name") or "Cliente"
    first_name = extract_safe_first_name(name) or "Cliente"

    return [
        p.replace("{name}", name).replace("{first_name}", first_name)
        for p in params
    ]


# ── Broadcast CRUD helpers ───────────────────────────────────

async def create_broadcast(
    name: str,
    template_name: str,
    target_tags: list[str],
    template_params: list[str] | None = None,
    target_channel: str = "whatsapp",
    scheduled_at: datetime | None = None,
) -> dict:
    """Create a new broadcast record."""
    target_channel = str(target_channel or "").strip().lower()
    if target_channel != "whatsapp":
        raise ValueError("Broadcasts can target only WhatsApp customers.")
    status = "scheduled" if scheduled_at else "draft"

    broadcast_id = await db.execute(
        """
        INSERT INTO broadcasts (name, template_name, template_params, target_tags,
                                target_channel, scheduled_at, status)
        VALUES (:name, :tmpl, :params, :tags, :channel, :sched, :status)
        RETURNING id
        """,
        {
            "name": name,
            "tmpl": template_name,
            "params": json.dumps(template_params) if template_params else None,
            "tags": json.dumps(target_tags),
            "channel": target_channel,
            "sched": scheduled_at,
            "status": status,
        },
    )

    return {
        "broadcast_id": str(broadcast_id),
        "name": name,
        "template_name": template_name,
        "target_tags": target_tags,
        "status": status,
    }


async def list_broadcasts(limit: int = 20) -> list[dict]:
    """List recent broadcasts."""
    rows = await db.fetch_all(
        """
        SELECT id, name, template_name, target_tags, target_channel,
               scheduled_at, sent_at, recipients, status
        FROM broadcasts
        ORDER BY COALESCE(scheduled_at, sent_at) DESC NULLS LAST
        LIMIT :limit
        """,
        {"limit": limit},
    )
    return [dict(r) for r in rows]


async def list_broadcast_deliveries(broadcast_id: str, limit: int = 500) -> list[dict]:
    """List durable recipient outcomes for audit and reconciliation."""
    rows = await db.fetch_all(
        """
        SELECT id, customer_id, platform_id, channel, display_name, status,
               attempt_count, claimed_at, sent_at, failed_at, last_error
        FROM broadcast_deliveries
        WHERE broadcast_id = :broadcast_id
        ORDER BY created_at, id
        LIMIT :limit
        """,
        {"broadcast_id": broadcast_id, "limit": max(1, min(limit, 1000))},
    )
    return [dict(row) for row in rows]


async def preview_broadcast(target_tags: list[str], channel: str = "whatsapp") -> dict:
    """Preview how many customers would receive a broadcast with these tags."""
    if str(channel or "").strip().lower() != "whatsapp":
        raise ValueError("Broadcasts can target only WhatsApp customers.")
    customers = await _query_customers_by_tags(target_tags, channel)
    return {
        "target_tags": target_tags,
        "channel": channel,
        "matching_customers": len(customers),
        "estimated_cost_usd": round(len(customers) * 0.0851, 2),
    }
