"""
Order creation and status management.
"""

import json
import logging
from app import db
from app.catalog.sheets import deduct_stock

logger = logging.getLogger(__name__)


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

    await db.execute(
        """
        UPDATE orders
        SET payment_status = :status, payment_proof = :note, updated_at = NOW()
        WHERE id = :oid
        """,
        {"status": status, "note": note, "oid": row["id"]},
    )

    return {"order_id": str(row["id"]), "payment_status": status}


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
