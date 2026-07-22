"""
Customer lookup, creation, and tag management.
"""

import json
import re
import unicodedata

from app import db
from app.customer_identity import normalize_phone_number


async def get_or_create_customer(
    channel: str,
    platform_id: str,
    display_name: str | None = None,
    phone: str | None = None,
    instagram_handle: str | None = None,
    allow_platform_phone_fallback: bool = True,
) -> dict:
    """
    Look up a customer by channel + platform_id.
    If they don't exist, create a new record tagged as 'new_lead'.
    Returns the customer row as a dict.
    """
    normalized_display_name = (display_name or "").strip() or None
    normalized_phone = normalize_phone_number(phone) or None
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
    normalized_phone = normalize_phone_number(phone)
    if not normalized_phone and allow_platform_phone_fallback and channel == "whatsapp":
        normalized_phone = normalize_phone_number(platform_id)

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
    merged = normalize_tags(existing + (new_tags or []))

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
    normalized_target = _normalize_tag(tag)
    filtered = [normalized for normalized in (_normalize_tag(t) for t in existing) if normalized and normalized != normalized_target]
    filtered = _dedupe_preserve_order(filtered)

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


def normalize_tags(tags: list[str] | None) -> list[str]:
    combined = []
    combined.extend(tags or [])
    normalized = [_normalize_tag(tag) for tag in combined]
    normalized = [tag for tag in normalized if tag]
    return _dedupe_preserve_order(normalized)


def _dedupe_preserve_order(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        if tag in seen:
            continue
        seen.add(tag)
        result.append(tag)
    return result


def _normalize_tag(tag: str | None) -> str:
    raw = str(tag or "").strip()
    if not raw:
        return ""

    if ":" not in raw:
        return _slugify_tag_value(raw)

    prefix, value = raw.split(":", 1)
    prefix = _slugify_tag_value(prefix)[:32]
    value = value.strip()
    if not prefix:
        return ""

    if prefix == "size":
        value = _slugify_tag_value(value).upper()
        return f"{prefix}:{value}" if value else ""

    value = _slugify_tag_value(value)
    return f"{prefix}:{value}" if value else ""


def _slugify_tag_value(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.lower())
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.replace("-", "_")
    ascii_text = re.sub(r"\s+", "_", ascii_text)
    ascii_text = re.sub(r"[^a-z0-9_]+", "_", ascii_text)
    ascii_text = re.sub(r"_+", "_", ascii_text).strip("_")
    return ascii_text[:64]
