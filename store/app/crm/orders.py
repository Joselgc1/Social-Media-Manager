"""
Order creation and status management.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app import db
from app.catalog.sheets import deduct_stock, restore_stock
from app.crm import customers

logger = logging.getLogger(__name__)

PAID_STATUSES = {"proof_received", "confirmed"}
VALID_PAYMENT_STATUSES = {"pending", "proof_received", "confirmed", "failed", "rejected"}
VALID_SHIPPING_STATUSES = {"pending", "shipped", "delivered"}


def _normalize_order_items(items: list[dict]) -> list[dict]:
    normalized_items: list[dict] = []
    for item in items:
        normalized_items.append({
            "product_name": str(item.get("product_name", "")).strip(),
            "sku": str(item.get("sku", "")).strip(),
            "size": str(item.get("size", "")).strip().upper(),
            "quantity": max(int(item.get("quantity", 1) or 1), 1),
            "unit_price": round(float(item.get("unit_price", 0) or 0), 2),
        })
    return normalized_items


def _load_items(raw_items: Any) -> list[dict]:
    if isinstance(raw_items, str):
        return json.loads(raw_items)
    return list(raw_items or [])


async def create_order(
    customer_id: str,
    items: list[dict],
    payment_method: str,
    shipping_city: str | None = None,
    shipping_address: str | None = None,
    shipping_method: str | None = None,
) -> dict:
    """
    Create a new order and return it as a dict.
    Reuses a matching recent open order to avoid duplicate orders from repeated tool calls
    or webhook retries.
    """
    normalized_items = _normalize_order_items(items)
    total = round(sum(item["unit_price"] * item["quantity"] for item in normalized_items), 2)
    items_json = json.dumps(normalized_items, ensure_ascii=False, sort_keys=True)

    created_new = False
    async with db.get_db().transaction():
        await db.fetch_one(
            "SELECT id FROM customers WHERE id = :id FOR UPDATE",
            {"id": customer_id},
        )

        existing = await db.fetch_one(
            """
            SELECT id, total, payment_status
            FROM orders
            WHERE customer_id = :cid
              AND items = CAST(:items AS jsonb)
              AND COALESCE(payment_method, '') = COALESCE(:pm, '')
              AND COALESCE(shipping_city, '') = COALESCE(:city, '')
              AND COALESCE(shipping_address, '') = COALESCE(:addr, '')
              AND COALESCE(shipping_method, '') = COALESCE(:method, '')
              AND payment_status IN ('pending', 'proof_received')
              AND created_at >= NOW() - INTERVAL '30 minutes'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            {
                "cid": customer_id,
                "items": items_json,
                "pm": payment_method,
                "city": shipping_city,
                "addr": shipping_address,
                "method": shipping_method,
            },
        )

        if existing:
            return {
                "order_id": str(existing["id"]),
                "items": normalized_items,
                "total": float(existing["total"]),
                "payment_method": payment_method,
                "shipping_city": shipping_city,
                "status": existing["payment_status"],
                "created_new": False,
            }

        order_id = await db.execute(
            """
            INSERT INTO orders (customer_id, items, total, payment_method, shipping_city, shipping_address, shipping_method)
            VALUES (:cid, CAST(:items AS jsonb), :total, :pm, :city, :addr, :sm)
            RETURNING id
            """,
            {
                "cid": customer_id,
                "items": items_json,
                "total": total,
                "pm": payment_method,
                "city": shipping_city,
                "addr": shipping_address,
                "sm": shipping_method,
            },
        )
        created_new = True

    if created_new:
        try:
            deduct_stock(normalized_items)
        except Exception as e:
            logger.error(f"Failed to deduct stock after order creation: {e}")

    return {
        "order_id": str(order_id),
        "items": normalized_items,
        "total": total,
        "payment_method": payment_method,
        "shipping_city": shipping_city,
        "status": "pending",
        "created_new": created_new,
    }


async def _apply_paid_customer_updates(customer_id: str, amount: float):
    """Increment paid-order totals and attach repeat-buyer tags when thresholds are met."""
    await customers.increment_orders(customer_id, amount)
    customer = await db.fetch_one(
        "SELECT total_orders, total_spent FROM customers WHERE id = :id",
        {"id": customer_id},
    )
    if not customer:
        return

    tags = []
    if customer["total_orders"] >= 2:
        tags.append("repeat_buyer")
    if customer["total_orders"] >= 3 or float(customer["total_spent"]) >= 100:
        tags.append("vip")
    if tags:
        await customers.add_tags(customer_id, tags)


async def get_order(order_id: str) -> dict | None:
    row = await db.fetch_one(
        """
        SELECT id, customer_id, items, total, payment_method, payment_status,
               customer_totals_applied, shipping_method, shipping_city, shipping_address,
               shipping_status, tracking_number, created_at, updated_at
        FROM orders
        WHERE id = :oid
        """,
        {"oid": order_id},
    )
    if not row:
        return None

    order = dict(row)
    order["id"] = str(order["id"])
    order["customer_id"] = str(order["customer_id"]) if order.get("customer_id") else None
    order["items"] = _load_items(order.get("items"))
    order["total"] = float(order.get("total") or 0)
    order["customer_totals_applied"] = bool(order.get("customer_totals_applied"))
    return order


async def get_latest_open_order(customer_id: str) -> dict | None:
    row = await db.fetch_one(
        """
        SELECT id
        FROM orders
        WHERE customer_id = :cid
          AND payment_status IN ('pending', 'proof_received')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"cid": customer_id},
    )
    if not row:
        return None
    return await get_order(str(row["id"]))


async def update_order_payment_status(order_id: str, status: str, note: str | None = None) -> dict | None:
    """
    Update an order payment status.
    Customer lifetime totals are applied exactly once, when the order first becomes paid.
    """
    if status not in VALID_PAYMENT_STATUSES:
        raise ValueError(f"Invalid payment status '{status}'")

    async with db.get_db().transaction():
        row = await db.fetch_one(
            """
            SELECT id, customer_id, total, payment_status, customer_totals_applied
            FROM orders
            WHERE id = :oid
            FOR UPDATE
            """,
            {"oid": order_id},
        )
        if not row:
            return None

        await db.execute(
            """
            UPDATE orders
            SET payment_status = :status,
                payment_proof = COALESCE(:note, payment_proof),
                updated_at = NOW()
            WHERE id = :oid
            """,
            {"status": status, "note": note, "oid": order_id},
        )

        applied_now = False
        if (
            status in PAID_STATUSES
            and not row["customer_totals_applied"]
            and row["customer_id"] is not None
        ):
            await _apply_paid_customer_updates(str(row["customer_id"]), float(row["total"]))
            await db.execute(
                """
                UPDATE orders
                SET customer_totals_applied = TRUE, updated_at = NOW()
                WHERE id = :oid
                """,
                {"oid": order_id},
            )
            applied_now = True

    return {
        "order_id": str(order_id),
        "payment_status": status,
        "customer_totals_applied": bool(row["customer_totals_applied"] or applied_now),
    }


async def update_payment_status(customer_id: str, status: str, note: str | None = None) -> dict | None:
    """
    Update the payment status of the customer's most recent open order.
    Returns the updated order or None if no open order was found.
    """
    order = await get_latest_open_order(customer_id)
    if not order:
        return None
    return await update_order_payment_status(order["id"], status=status, note=note)


async def update_order_shipping(
    order_id: str,
    shipping_status: str | None = None,
    tracking_number: str | None = None,
) -> dict | None:
    """Update shipping status and/or tracking number for an order."""
    if shipping_status is not None and shipping_status not in VALID_SHIPPING_STATUSES:
        raise ValueError(f"Invalid shipping status '{shipping_status}'")

    row = await db.fetch_one(
        "SELECT id FROM orders WHERE id = :oid",
        {"oid": order_id},
    )
    if not row:
        return None

    await db.execute(
        """
        UPDATE orders
        SET shipping_status = COALESCE(:shipping_status, shipping_status),
            tracking_number = :tracking_number,
            updated_at = NOW()
        WHERE id = :oid
        """,
        {
            "shipping_status": shipping_status,
            "tracking_number": (tracking_number or "").strip() or None,
            "oid": order_id,
        },
    )

    updated = await get_order(order_id)
    if not updated:
        return None
    return {
        "order_id": updated["id"],
        "shipping_status": updated["shipping_status"],
        "tracking_number": updated["tracking_number"],
    }


async def delete_order(order_id: str) -> dict | None:
    """Delete an order, restore inventory, and roll back applied customer totals if needed."""
    async with db.get_db().transaction():
        row = await db.fetch_one(
            """
            SELECT id, customer_id, items, total, customer_totals_applied
            FROM orders
            WHERE id = :oid
            FOR UPDATE
            """,
            {"oid": order_id},
        )
        if not row:
            return None

        items = _load_items(row["items"])

        if row["customer_totals_applied"] and row["customer_id"]:
            await customers.decrement_orders(str(row["customer_id"]), float(row["total"]))

        await db.execute(
            "DELETE FROM orders WHERE id = :oid",
            {"oid": order_id},
        )

    try:
        restore_stock(items)
    except Exception as e:
        logger.error(f"Failed to restore stock after deleting order {order_id}: {e}")

    return {
        "order_id": str(order_id),
        "deleted": True,
        "restored_items": len(items),
    }


async def get_customer_orders(customer_id: str, limit: int = 5) -> list[dict]:
    """Get the customer's most recent orders."""
    rows = await db.fetch_all(
        """
        SELECT id, items, total, payment_method, payment_status,
               shipping_status, tracking_number, created_at
        FROM orders
        WHERE customer_id = :cid
        ORDER BY created_at DESC
        LIMIT :limit
        """,
        {"cid": customer_id, "limit": limit},
    )
    return [dict(r) for r in rows]
