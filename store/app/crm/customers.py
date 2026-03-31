"""
Customer lookup, creation, and tag management.
"""

import json
from app import db


async def get_or_create_customer(channel: str, platform_id: str, display_name: str | None = None) -> dict:
    """
    Look up a customer by channel + platform_id.
    If they don't exist, create a new record tagged as 'new_lead'.
    Returns the customer row as a dict.
    """
    row = await db.fetch_one(
        "SELECT * FROM customers WHERE channel = :channel AND platform_id = :pid",
        {"channel": channel, "pid": platform_id},
    )

    if row:
        # Update last_active timestamp
        await db.execute(
            "UPDATE customers SET last_active = NOW() WHERE id = :id",
            {"id": row["id"]},
        )
        return dict(row)

    # New customer
    new_id = await db.execute(
        """
        INSERT INTO customers (channel, platform_id, display_name, phone, tags)
        VALUES (:channel, :pid, :name, :phone, :tags)
        RETURNING id
        """,
        {
            "channel": channel,
            "pid": platform_id,
            "name": display_name,
            "phone": platform_id if channel == "whatsapp" else None,
            "tags": json.dumps(["new_lead"]),
        },
    )

    row = await db.fetch_one("SELECT * FROM customers WHERE id = :id", {"id": new_id})
    return dict(row)


async def add_tags(customer_id: str, new_tags: list[str]):
    """
    Append tags to a customer's tag list (deduplicated).
    Tags are stored as a JSONB array.
    """
    row = await db.fetch_one(
        "SELECT tags FROM customers WHERE id = :id",
        {"id": customer_id},
    )
    if not row:
        return

    existing = json.loads(row["tags"]) if isinstance(row["tags"], str) else (row["tags"] or [])
    merged = list(set(existing + new_tags))

    await db.execute(
        "UPDATE customers SET tags = :tags WHERE id = :id",
        {"tags": json.dumps(merged), "id": customer_id},
    )


async def remove_tag(customer_id: str, tag: str):
    """Remove a single tag from a customer."""
    row = await db.fetch_one(
        "SELECT tags FROM customers WHERE id = :id",
        {"id": customer_id},
    )
    if not row:
        return

    existing = json.loads(row["tags"]) if isinstance(row["tags"], str) else (row["tags"] or [])
    filtered = [t for t in existing if t != tag]

    await db.execute(
        "UPDATE customers SET tags = :tags WHERE id = :id",
        {"tags": json.dumps(filtered), "id": customer_id},
    )


async def set_conversation_state(customer_id: str, state: str):
    """Set the conversation state (active, escalated, blocked)."""
    await db.execute(
        "UPDATE customers SET conversation_state = :state WHERE id = :id",
        {"state": state, "id": customer_id},
    )


async def increment_orders(customer_id: str, amount: float):
    """Increment order count and total spent after a confirmed purchase."""
    await db.execute(
        """
        UPDATE customers
        SET total_orders = total_orders + 1,
            total_spent = total_spent + :amount
        WHERE id = :id
        """,
        {"amount": amount, "id": customer_id},
    )
