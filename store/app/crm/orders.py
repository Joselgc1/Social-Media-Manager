"""
Order creation and status management.
"""

import json
import logging
from app import db
from app.catalog.sheets import deduct_stock
from app.crm import customers

logger = logging.getLogger(__name__)

PAID_STATUSES = {"proof_received", "confirmed"}


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

    Parameters
    ----------
    items : list of dicts with keys: product_name, sku, size, quantity, unit_price
    """
    total = sum(item["unit_price"] * item["quantity"] for item in items)

    order_id = await db.execute(
        """
        INSERT INTO orders (customer_id, items, total, payment_method, shipping_city, shipping_address, shipping_method)
        VALUES (:cid, :items, :total, :pm, :city, :addr, :sm)
        RETURNING id
        """,
        {
            "cid": customer_id,
            "items": json.dumps(items),
            "total": total,
            "pm": payment_method,
            "city": shipping_city,
            "addr": shipping_address,
            "sm": shipping_method,
        },
    )

    # Deduct stock from Google Sheets
    try:
        deduct_stock(items)
    except Exception as e:
        logger.error(f"Failed to deduct stock after order creation: {e}")

    return {
        "order_id": str(order_id),
        "items": items,
        "total": total,
        "payment_method": payment_method,
        "shipping_city": shipping_city,
        "status": "pending",
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


async def update_order_payment_status(order_id: str, status: str, note: str | None = None) -> dict | None:
    """
    Update an order payment status.
    Customer lifetime totals are applied exactly once, when the order first becomes paid.
    """
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
    Update the payment status of the customer's most recent pending order.
    Returns the updated order or None if no pending order was found.
    """
    row = await db.fetch_one(
        """
        SELECT id FROM orders
        WHERE customer_id = :cid AND payment_status = 'pending'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"cid": customer_id},
    )

    if not row:
        return None

    return await update_order_payment_status(str(row["id"]), status=status, note=note)


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
