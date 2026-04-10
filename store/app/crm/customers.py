"""
Customer lookup, creation, and tag management.
"""

import json
from app import db


async def get_or_create_customer(
    channel: str,
    platform_id: str,
    display_name: str | None = None,
    phone: str | None = None,
    instagram_handle: str | None = None,
) -> dict:
    """
    Look up a customer by channel + platform_id.
    If they don't exist, create a new record tagged as 'new_lead'.
    Returns the customer row as a dict.
    """
    normalized_display_name = (display_name or "").strip() or None
    normalized_phone = (phone or "").strip() or None
    normalized_instagram_handle = (instagram_handle or "").strip().lstrip("@") or None

    if channel == "instagram" and not normalized_display_name and normalized_instagram_handle:
        normalized_display_name = f"@{normalized_instagram_handle}"

    row = await db.fetch_one(
        "SELECT * FROM customers WHERE channel = :channel AND platform_id = :pid",
        {"channel": channel, "pid": platform_id},
    )

    if row:
        updates = {"id": row["id"]}
        set_clauses = ["last_active = NOW()"]
        current_display_name = (row["display_name"] or "").strip() or None

        if (
            normalized_instagram_handle
            and channel == "instagram"
            and not normalized_display_name
            and (not current_display_name or current_display_name == row["platform_id"])
        ):
            normalized_display_name = f"@{normalized_instagram_handle}"

        if normalized_display_name and normalized_display_name != row["display_name"]:
            updates["display_name"] = normalized_display_name
            set_clauses.append("display_name = :display_name")

        if normalized_phone and normalized_phone != row["phone"]:
            updates["phone"] = normalized_phone
            set_clauses.append("phone = :phone")

        if normalized_instagram_handle and normalized_instagram_handle != row["instagram_handle"]:
            updates["instagram_handle"] = normalized_instagram_handle
            set_clauses.append("instagram_handle = :instagram_handle")

        await db.execute(
            f"UPDATE customers SET {', '.join(set_clauses)} WHERE id = :id",
            updates,
        )
        refreshed = await db.fetch_one("SELECT * FROM customers WHERE id = :id", {"id": row["id"]})
        return dict(refreshed)

    # New customer
    normalized_phone = (phone or "").strip() or (platform_id if channel == "whatsapp" else None)

    new_id = await db.execute(
        """
        INSERT INTO customers (channel, platform_id, display_name, phone, instagram_handle, tags)
        VALUES (:channel, :pid, :name, :phone, :instagram_handle, :tags)
        RETURNING id
        """,
        {
            "channel": channel,
            "pid": platform_id,
            "name": normalized_display_name,
            "phone": normalized_phone,
            "instagram_handle": normalized_instagram_handle,
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


async def decrement_orders(customer_id: str, amount: float):
    """Undo a previously applied paid-order total adjustment."""
    await db.execute(
        """
        UPDATE customers
        SET total_orders = GREATEST(total_orders - 1, 0),
            total_spent = GREATEST(total_spent - :amount, 0)
        WHERE id = :id
        """,
        {"amount": amount, "id": customer_id},
    )


async def update_customer(customer_id: str, channel: str | None = None, conversation_state: str | None = None) -> dict | None:
    row = await db.fetch_one(
        "SELECT * FROM customers WHERE id::text = :id",
        {"id": customer_id},
    )
    if not row:
        return None

    updates = {"id": customer_id}
    set_clauses = []

    if channel is not None:
        updates["channel"] = channel
        set_clauses.append("channel = :channel")

    if conversation_state is not None:
        updates["state"] = conversation_state
        set_clauses.append("conversation_state = :state")

    if not set_clauses:
        return dict(row)

    await db.execute(
        f"UPDATE customers SET {', '.join(set_clauses)} WHERE id::text = :id",
        updates,
    )
    updated = await db.fetch_one("SELECT * FROM customers WHERE id::text = :id", {"id": customer_id})
    return dict(updated) if updated else None


async def delete_customer(customer_id: str) -> bool:
    row = await db.fetch_one(
        "SELECT id FROM customers WHERE id::text = :id",
        {"id": customer_id},
    )
    if not row:
        return False

    await db.execute(
        "DELETE FROM customers WHERE id::text = :id",
        {"id": customer_id},
    )
    return True
