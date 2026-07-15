"""
Read-only support tool handlers scoped to the active customer.
"""

from __future__ import annotations

import json
from typing import Any

from app.ai.tools.context import ToolExecutionContext
from app.crm import orders


async def get_customer_profile(args: dict, context: ToolExecutionContext) -> dict:
    """Return safe profile context for the active customer only."""
    customer = context.customer or {}
    return {
        "status": "ok",
        "profile": {
            "display_name": customer.get("display_name"),
            "conversation_state": customer.get("conversation_state"),
            "total_orders": _safe_int(customer.get("total_orders")),
            "total_spent": _safe_float(customer.get("total_spent")),
            "last_shipping_city": customer.get("last_shipping_city"),
            "last_shipping_method": customer.get("last_shipping_method"),
            "has_saved_shipping_address": bool(customer.get("last_shipping_address")),
        },
    }


async def get_customer_order_status(args: dict, context: ToolExecutionContext) -> dict:
    """Return sanitized recent-order status for the active customer only."""
    limit = _clamp_int(args.get("limit"), default=3, minimum=1, maximum=5)
    rows = await orders.get_customer_orders(context.customer["id"], limit=limit)
    sanitized = [_sanitize_order(row, index) for index, row in enumerate(rows or [], start=1)]
    return {
        "status": "ok",
        "orders": sanitized,
        "count": len(sanitized),
    }


def _sanitize_order(order: dict[str, Any], index: int) -> dict:
    return {
        "reference": f"recent_order_{index}",
        "items_summary": _items_summary(order.get("items")),
        "total": _safe_float(order.get("total")),
        "payment_method": order.get("payment_method"),
        "payment_status": order.get("payment_status"),
        "shipping_status": order.get("shipping_status"),
        "tracking_number": order.get("tracking_number") or None,
        "created_at": str(order.get("created_at")) if order.get("created_at") else None,
    }


def _items_summary(raw_items) -> str:
    if isinstance(raw_items, str):
        try:
            raw_items = json.loads(raw_items or "[]")
        except json.JSONDecodeError:
            raw_items = []
    if not isinstance(raw_items, list):
        raw_items = []
    parts = []
    for item in raw_items[:3]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("product_name") or item.get("name") or "Producto").strip() or "Producto"
        quantity = _safe_int(item.get("quantity") or item.get("qty"), default=1)
        size = str(item.get("size") or "").strip().upper()
        size_text = f" talla {size}" if size else ""
        parts.append(f"{name}{size_text} x{quantity}")
    return ", ".join(parts)


def _clamp_int(value, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return round(float(value or default), 2)
    except (TypeError, ValueError):
        return default
