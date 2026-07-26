"""
Order creation and status management.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from contextlib import asynccontextmanager
from typing import Any

from app import db
from app.catalog.sheets import (
    deduct_stock,
    ensure_fresh_catalog,
    get_cached_catalog,
    get_product_sizes,
    inventory_operation_applied,
    restore_stock,
)
from app.crm import customers
from app.payment_methods import normalize_payment_methods
from app.runtime_settings import RUNTIME_SETTING_DEFAULTS

logger = logging.getLogger(__name__)

PAID_STATUSES = {"proof_received", "confirmed"}
VALID_PAYMENT_STATUSES = {"pending", "proof_received", "confirmed", "failed", "rejected"}
VALID_SHIPPING_STATUSES = {"pending", "shipped", "delivered"}
ORDER_DISCOUNT_THRESHOLD = float(RUNTIME_SETTING_DEFAULTS["order_discount_threshold_usd"])
ORDER_DISCOUNT_RATE = float(RUNTIME_SETTING_DEFAULTS["order_discount_percent"]) / 100.0
INVENTORY_LOCK_KEY = "store_inventory_google_sheets"
INVENTORY_RESERVATION_TTL_HOURS = 48
MAX_ACTIVE_UNPAID_ORDERS = 3
RESERVATION_IN_PROGRESS = "reservation_pending"
RESERVATION_FAILED = "reservation_failed"
RELEASE_IN_PROGRESS = "release_pending"
RELEASED = "released"


class InventoryReservationUncertainError(RuntimeError):
    """The Sheets mutation outcome could not be reconciled with its ledger."""


class PendingOrderLimitError(ValueError):
    """Raised when a customer has reached the unpaid-order limit."""

    def __init__(self, pending_order_count: int):
        self.pending_order_count = pending_order_count
        super().__init__(
            f"Tienes {pending_order_count} pedidos pendientes de pago. "
            "Paga o envía el comprobante de alguno antes de crear otro pedido."
        )


def _normalize_order_items(items: list[dict]) -> list[dict]:
    normalized_items: list[dict] = []
    for item in items:
        requested_size = str(item.get("size", "")).strip().upper()
        variant = _resolve_catalog_variant(
            sku=str(item.get("sku", "")).strip(),
            product_name=str(item.get("product_name", "")).strip(),
            size=requested_size,
        )
        if not variant:
            raise ValueError("Product or size was not found in the current catalog.")
        quantity = max(int(item.get("quantity", 1) or 1), 1)
        available_stock = _catalog_stock(variant)
        if available_stock is not None and quantity > available_stock:
            raise ValueError("Requested quantity is not available in the current catalog.")

        normalized_items.append({
            "product_name": str(variant.get("product_name") or "").strip(),
            "sku": str(variant.get("sku") or "").strip(),
            "size": requested_size or _first_catalog_size(variant),
            "quantity": quantity,
            "unit_price": round(float(variant.get("price_usd") or 0), 2),
        })
    return normalized_items


def _load_items(raw_items: Any) -> list[dict]:
    if isinstance(raw_items, str):
        return json.loads(raw_items)
    return list(raw_items or [])


def _coerce_non_negative_float(value, default: float) -> float:
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return default


def _resolve_order_discount_config(settings: dict | None = None) -> dict:
    source = settings or {}
    threshold = _coerce_non_negative_float(
        source.get("order_discount_threshold_usd"),
        ORDER_DISCOUNT_THRESHOLD,
    )
    percent = _coerce_non_negative_float(
        source.get("order_discount_percent"),
        ORDER_DISCOUNT_RATE * 100.0,
    )
    return {
        "threshold_usd": round(threshold, 2),
        "percent": round(percent, 2),
        "rate": round(percent / 100.0, 4),
    }


def _calculate_order_amounts(items: list[dict], settings: dict | None = None) -> dict:
    subtotal = round(sum(float(item.get("unit_price", 0)) * int(item.get("quantity", 0)) for item in items), 2)
    discount_config = _resolve_order_discount_config(settings)
    discount_applied = (
        discount_config["percent"] > 0
        and discount_config["threshold_usd"] > 0
        and subtotal > discount_config["threshold_usd"]
    )
    discount_amount = round(subtotal * discount_config["rate"], 2) if discount_applied else 0.0
    total = round(subtotal - discount_amount, 2)
    return {
        "subtotal": subtotal,
        "discount_applied": discount_applied,
        "discount_rate": discount_config["rate"] if discount_applied else 0.0,
        "discount_percent": discount_config["percent"] if discount_applied else 0.0,
        "discount_threshold_usd": discount_config["threshold_usd"],
        "discount_amount": discount_amount,
        "total": total,
    }


def _hydrate_order_pricing(order: dict) -> dict:
    """
    Attach pricing breakdown fields to an order using its stored items and total.

    This keeps order detail backward-compatible for existing rows even when
    discount metadata was not persisted separately.
    """
    items = _load_items(order.get("items"))
    subtotal = round(
        sum(float(item.get("unit_price", 0)) * int(item.get("quantity", 0)) for item in items),
        2,
    )
    total = round(float(order.get("total") or 0), 2)
    shipping_fee = round(float(order.get("shipping_fee") or 0), 2)
    stored_merchandise_total = order.get("merchandise_total")
    merchandise_total = round(
        float(stored_merchandise_total) if stored_merchandise_total is not None else max(total - shipping_fee, 0.0),
        2,
    )
    discount_amount = round(max(subtotal - merchandise_total, 0.0), 2)
    discount_applied = discount_amount > 0
    discount_percent = round((discount_amount / subtotal) * 100, 2) if discount_applied and subtotal > 0 else 0.0
    discount_rate = round(discount_percent / 100.0, 4) if discount_applied else 0.0

    order["items"] = items
    order["subtotal"] = subtotal
    order["merchandise_total"] = merchandise_total
    order["shipping_fee"] = shipping_fee
    order["shipping_currency"] = order.get("shipping_currency") or "USD"
    order["discount_applied"] = discount_applied
    order["discount_amount"] = discount_amount
    order["discount_percent"] = discount_percent
    order["discount_rate"] = discount_rate
    return order


def _customer_spend_amount(order) -> float:
    """Exclude prepaid delivery from lifetime product spend when available."""
    try:
        merchandise_total = order["merchandise_total"]
    except KeyError:
        merchandise_total = None
    return float(merchandise_total) if merchandise_total is not None else float(order["total"])


def _resolve_catalog_variant(sku: str, product_name: str, size: str) -> dict | None:
    normalized_sku = (sku or "").strip()
    normalized_name = _normalize_catalog_text(product_name)
    normalized_size = (size or "").strip().upper()

    catalog = get_cached_catalog() or []

    if normalized_sku:
        direct_match = next(
            (
                product for product in catalog
                if str(product.get("sku", "")).strip() == normalized_sku
                and _catalog_variant_matches_size(product, normalized_size)
            ),
            None,
        )
        if direct_match:
            return direct_match

        parent_match = next(
            (
                product for product in catalog
                if str(product.get("parent_sku", "")).strip() == normalized_sku
                and _catalog_variant_matches_size(product, normalized_size)
            ),
            None,
        )
        if parent_match:
            return parent_match

    if normalized_name:
        name_matches = [
            product
            for product in catalog
            if _normalize_catalog_text(product.get("product_name", "")) == normalized_name
            and _catalog_variant_matches_size(product, normalized_size)
        ]
        if len(name_matches) == 1:
            return name_matches[0]
        if name_matches:
            return name_matches[0]

    return None


def _catalog_variant_matches_size(product: dict, size: str) -> bool:
    sizes = get_product_sizes(product)
    if not size:
        return True
    return size in sizes if sizes else str(product.get("size", "")).strip().upper() == size


def _first_catalog_size(product: dict | None) -> str:
    sizes = get_product_sizes(product or {})
    return sizes[0] if sizes else ""


def _catalog_stock(product: dict) -> int | None:
    try:
        return int(float(product.get("stock")))
    except (TypeError, ValueError):
        return None


def _normalize_catalog_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(text or "").lower())
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip()


@asynccontextmanager
async def _inventory_mutation_connection():
    """Hold a session advisory lock while coordinating DB state with Sheets."""
    async with db.get_db().connection() as connection:
        await connection.fetch_one(
            "SELECT pg_advisory_lock(hashtextextended(:key, 0))",
            {"key": INVENTORY_LOCK_KEY},
        )
        try:
            yield connection
        finally:
            try:
                await connection.fetch_one(
                    "SELECT pg_advisory_unlock(hashtextextended(:key, 0))",
                    {"key": INVENTORY_LOCK_KEY},
                )
            except Exception:
                logger.exception("Failed to release inventory advisory lock")


def _reserve_operation_id(order_id: str) -> str:
    return f"order:{order_id}:reserve"


def _release_operation_id(order_id: str) -> str:
    return f"order:{order_id}:release"


async def _run_sheet_operation(operation, *args, **kwargs):
    """Wait for a blocking Sheets call even when the request is cancelled.

    The advisory lock must remain held until the worker thread has stopped mutating
    Sheets. Cancellation is re-raised only after that point.
    """
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        try:
            task.result()
        except Exception:
            logger.exception("Sheets operation failed after request cancellation")
        raise


async def _inventory_operation_applied(order_id: str, items: list[dict], direction: int) -> bool:
    operation_id = _reserve_operation_id(order_id) if direction == -1 else _release_operation_id(order_id)
    return await _run_sheet_operation(
        inventory_operation_applied,
        operation_id,
        items,
        direction=direction,
    )


async def _deduct_order_inventory(order_id: str, items: list[dict]) -> None:
    operation_id = _reserve_operation_id(order_id)
    try:
        await _run_sheet_operation(deduct_stock, items, operation_id=operation_id)
    except Exception:
        try:
            applied = await _inventory_operation_applied(order_id, items, -1)
        except Exception as verify_error:
            logger.exception("Failed to verify inventory reservation operation %s", operation_id)
            raise InventoryReservationUncertainError(
                "The inventory reservation outcome could not be verified; retry the existing order later."
            ) from verify_error
        if applied:
            logger.warning("Inventory reservation operation %s succeeded after a reported Sheets error", operation_id)
            return
        raise


async def _restore_order_inventory(order_id: str, items: list[dict]) -> None:
    operation_id = _release_operation_id(order_id)
    try:
        await _run_sheet_operation(restore_stock, items, operation_id=operation_id)
    except Exception as mutation_error:
        try:
            applied = await _inventory_operation_applied(order_id, items, 1)
        except Exception as verify_error:
            logger.exception("Failed to verify inventory release operation %s", operation_id)
            raise mutation_error from verify_error
        if applied:
            logger.warning("Inventory release operation %s succeeded after a reported Sheets error", operation_id)
            return
        raise


async def _mark_inventory_reservation_failed(connection, order_id: str) -> None:
    async with connection.transaction():
        await connection.execute(
            """
            UPDATE orders
            SET inventory_status = :failed_status,
                payment_status = CASE
                    WHEN payment_status = 'pending' THEN 'rejected'
                    ELSE payment_status
                END,
                updated_at = NOW()
            WHERE id = :oid
              AND inventory_status = :pending_status
            """,
            {
                "oid": order_id,
                "failed_status": RESERVATION_FAILED,
                "pending_status": RESERVATION_IN_PROGRESS,
            },
        )


async def create_order(
    customer_id: str,
    items: list[dict],
    payment_method: str,
    shipping_city: str | None = None,
    shipping_address: str | None = None,
    shipping_method: str | None = None,
    fulfillment_type: str | None = None,
    shipping_zone: str | None = None,
    pickup_agency: str | None = None,
    shipping_fee: float | int | str = 0,
    shipping_currency: str = "USD",
) -> dict:
    """
    Create a new order and return it as a dict.
    Reuses a matching recent open order to avoid duplicate orders from repeated tool calls
    or webhook retries.
    """
    if not isinstance(items, list) or not items or any(not isinstance(item, dict) for item in items):
        raise ValueError("Order must contain at least one item.")

    required_fields = {"payment method": payment_method, "shipping city": shipping_city}
    if fulfillment_type == "home_delivery":
        required_fields["shipping address"] = shipping_address
        required_fields["shipping zone"] = shipping_zone
    elif fulfillment_type == "courier_agency_pickup":
        required_fields["shipping method"] = shipping_method
        required_fields["pickup agency"] = pickup_agency
    else:
        required_fields["shipping address"] = shipping_address
        required_fields["shipping method"] = shipping_method
    missing_fields = [name for name, value in required_fields.items() if not isinstance(value, str) or not value.strip()]
    if missing_fields:
        raise ValueError(f"Missing required order field: {missing_fields[0]}.")
    if shipping_method and shipping_method.strip().lower() not in {"mrw", "zoom"}:
        raise ValueError("Shipping method must be MRW or Zoom.")

    settings = await db.get_settings()
    configured_methods = normalize_payment_methods(settings.get("payment_methods") or [])
    normalized_payment_method = _normalize_catalog_text(payment_method)
    matching_method = next(
        (
            method["name"]
            for method in configured_methods
            if _normalize_catalog_text(method["name"]) == normalized_payment_method
        ),
        None,
    )
    if not matching_method:
        raise ValueError("Payment method is not configured for this store.")

    payment_method = matching_method
    shipping_city = shipping_city.strip()
    shipping_address = (shipping_address or "").strip() or None
    shipping_method = (shipping_method or "").strip().lower() or None
    shipping_zone = (shipping_zone or "").strip() or None
    pickup_agency = (pickup_agency or "").strip() or None
    await ensure_fresh_catalog()
    normalized_items = _normalize_order_items(items)
    pricing = _calculate_order_amounts(normalized_items, settings=settings)
    merchandise_total = pricing["total"]
    delivery_fee = _coerce_non_negative_float(shipping_fee, 0.0)
    total = round(merchandise_total + delivery_fee, 2)
    currency = str(shipping_currency or "USD").strip().upper() or "USD"
    items_json = json.dumps(normalized_items, ensure_ascii=False, sort_keys=True)

    customer_exists = await db.fetch_one(
        "SELECT id FROM customers WHERE id = :id",
        {"id": customer_id},
    )
    if not customer_exists:
        raise ValueError("Customer was not found.")

    created_new = False
    order_id: str | None = None
    order_status = "pending"
    order_total = total
    inventory_status = RESERVATION_IN_PROGRESS

    async with _inventory_mutation_connection() as connection:
        async with connection.transaction():
            existing = await connection.fetch_one(
                """
                SELECT id, total, payment_status, inventory_status
                FROM orders
                WHERE customer_id = :cid
                  AND items = CAST(:items AS jsonb)
                  AND COALESCE(payment_method, '') = COALESCE(:pm, '')
                  AND COALESCE(shipping_city, '') = COALESCE(:city, '')
                  AND COALESCE(shipping_address, '') = COALESCE(:addr, '')
                  AND COALESCE(shipping_method, '') = COALESCE(:method, '')
                  AND COALESCE(fulfillment_type, '') = COALESCE(:fulfillment_type, '')
                  AND COALESCE(shipping_zone, '') = COALESCE(:shipping_zone, '')
                  AND COALESCE(pickup_agency, '') = COALESCE(:pickup_agency, '')
                  AND COALESCE(shipping_fee, 0) = :shipping_fee
                  AND payment_status IN ('pending', 'proof_received')
                  AND inventory_status IN (:reservation_pending, 'reserved')
                  AND (
                      inventory_status = :reservation_pending
                      OR created_at >= NOW() - INTERVAL '30 minutes'
                  )
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
                    "fulfillment_type": fulfillment_type,
                    "shipping_zone": shipping_zone,
                    "pickup_agency": pickup_agency,
                    "shipping_fee": delivery_fee,
                    "reservation_pending": RESERVATION_IN_PROGRESS,
                },
            )

            if existing:
                order_id = str(existing["id"])
                order_total = float(existing["total"])
                order_status = existing["payment_status"]
                inventory_status = existing["inventory_status"]
            else:
                pending_order_count = await _get_active_unpaid_order_count(connection, customer_id)
                if pending_order_count >= MAX_ACTIVE_UNPAID_ORDERS:
                    raise PendingOrderLimitError(pending_order_count)
                order_id = str(await connection.execute(
                    """
                    INSERT INTO orders (
                        customer_id, items, total, payment_method, shipping_city,
                        shipping_address, shipping_method, fulfillment_type, shipping_zone,
                        pickup_agency, merchandise_total, shipping_fee, shipping_currency, inventory_status
                    )
                    VALUES (
                        :cid, CAST(:items AS jsonb), :total, :pm, :city,
                        :addr, :sm, :fulfillment_type, :shipping_zone,
                        :pickup_agency, :merchandise_total, :shipping_fee, :shipping_currency, :inventory_status
                    )
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
                        "fulfillment_type": fulfillment_type,
                        "shipping_zone": shipping_zone,
                        "pickup_agency": pickup_agency,
                        "merchandise_total": merchandise_total,
                        "shipping_fee": delivery_fee,
                        "shipping_currency": currency,
                        "inventory_status": RESERVATION_IN_PROGRESS,
                    },
                ))
                created_new = True

        if inventory_status == "reserved":
            return {
                "order_id": str(order_id),
                "items": normalized_items,
                "total": order_total,
                "subtotal": pricing["subtotal"],
                "merchandise_total": merchandise_total,
                "shipping_fee": delivery_fee,
                "shipping_currency": currency,
                "discount_applied": pricing["discount_applied"],
                "discount_rate": pricing["discount_rate"],
                "discount_percent": pricing["discount_percent"],
                "discount_threshold_usd": pricing["discount_threshold_usd"],
                "discount_amount": pricing["discount_amount"],
                "payment_method": payment_method,
                "shipping_city": shipping_city,
                "shipping_address": shipping_address,
                "shipping_method": shipping_method,
                "fulfillment_type": fulfillment_type,
                "shipping_zone": shipping_zone,
                "pickup_agency": pickup_agency,
                "status": order_status,
                "created_new": False,
            }

        if inventory_status != RESERVATION_IN_PROGRESS:
            raise RuntimeError(
                f"Order {order_id} is in inventory state '{inventory_status}' and cannot be reserved."
            )

        try:
            await _deduct_order_inventory(order_id, normalized_items)
        except InventoryReservationUncertainError:
            # The Sheets call may have committed. Keep its operation ID pending so
            # retries and the cleanup job reconcile it rather than deducting again.
            raise
        except Exception:
            try:
                await _mark_inventory_reservation_failed(connection, order_id)
            except Exception:
                logger.exception("Failed to mark order %s inventory reservation as failed", order_id)
            raise

        async with connection.transaction():
            current = await connection.fetch_one(
                """
                SELECT id, payment_status, inventory_status
                FROM orders
                WHERE id = :oid
                FOR UPDATE
                """,
                {"oid": order_id},
            )
            if not current:
                raise RuntimeError(
                    "Inventory was reserved in Google Sheets, but the order row is missing; manual reconciliation is required."
                )
            if current["inventory_status"] == RESERVATION_IN_PROGRESS:
                await connection.execute(
                    """
                    UPDATE orders
                    SET inventory_status = 'reserved',
                        inventory_reserved_at = NOW(),
                        updated_at = NOW()
                    WHERE id = :oid
                    """,
                    {"oid": order_id},
                )
                inventory_status = "reserved"
            elif current["inventory_status"] == "reserved":
                inventory_status = "reserved"
            else:
                raise RuntimeError(
                    f"Inventory was reserved, but order {order_id} is in state '{current['inventory_status']}'; "
                    "manual reconciliation is required."
                )
            order_status = current["payment_status"]

    return {
        "order_id": str(order_id),
        "items": normalized_items,
        "total": order_total,
        "subtotal": pricing["subtotal"],
        "merchandise_total": merchandise_total,
        "shipping_fee": delivery_fee,
        "shipping_currency": currency,
        "discount_applied": pricing["discount_applied"],
        "discount_rate": pricing["discount_rate"],
        "discount_percent": pricing["discount_percent"],
        "discount_threshold_usd": pricing["discount_threshold_usd"],
        "discount_amount": pricing["discount_amount"],
        "payment_method": payment_method,
        "shipping_city": shipping_city,
        "shipping_address": shipping_address,
        "shipping_method": shipping_method,
        "fulfillment_type": fulfillment_type,
        "shipping_zone": shipping_zone,
        "pickup_agency": pickup_agency,
        "status": order_status,
        "created_new": created_new,
    }


async def _apply_paid_customer_updates(customer_id: str, amount: float):
    """Increment paid-order totals and attach repeat-buyer tags when thresholds are met."""
    await customers.increment_orders(customer_id, amount)
    await customers.sync_purchase_tier_tags(customer_id)


async def _revert_paid_customer_updates(customer_id: str, amount: float):
    """Reverse paid-order totals and remove tier tags that no longer qualify."""
    await customers.decrement_orders(customer_id, amount)
    await customers.sync_purchase_tier_tags(customer_id)


async def get_order(order_id: str) -> dict | None:
    row = await db.fetch_one(
        """
        SELECT id, customer_id, items, total, currency, merchandise_total, shipping_fee, shipping_currency,
               payment_method, payment_status, customer_totals_applied, shipping_method, shipping_city, shipping_address,
               fulfillment_type, shipping_zone, pickup_agency,
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
    order["total"] = float(order.get("total") or 0)
    order["customer_totals_applied"] = bool(order.get("customer_totals_applied"))
    return _hydrate_order_pricing(order)


async def get_order_detail(order_id: str) -> dict | None:
    row = await db.fetch_one(
        """
        SELECT o.id, o.customer_id, o.items, o.total, o.currency, o.merchandise_total, o.shipping_fee,
               o.shipping_currency, o.payment_method, o.payment_status, o.payment_proof,
               o.customer_totals_applied, o.shipping_method, o.shipping_city, o.shipping_address,
               o.fulfillment_type, o.shipping_zone, o.pickup_agency, o.shipping_status,
               o.tracking_number, o.created_at, o.updated_at,
               c.id AS customer_record_id, c.channel, c.platform_id, c.display_name, c.phone,
               c.instagram_handle, c.tags, c.total_orders, c.total_spent, c.first_contact,
               c.last_active, c.notes, c.conversation_state, c.last_shipping_address,
               c.last_shipping_city, c.last_shipping_method, c.last_fulfillment_type,
               c.last_shipping_zone, c.last_pickup_agency
        FROM orders o
        LEFT JOIN customers c ON o.customer_id = c.id
        WHERE o.id = :oid
        """,
        {"oid": order_id},
    )
    if not row:
        return None

    detail = dict(row)
    detail["id"] = str(detail["id"])
    detail["customer_id"] = str(detail["customer_id"]) if detail.get("customer_id") else None
    detail["total"] = float(detail.get("total") or 0)
    detail["customer_totals_applied"] = bool(detail.get("customer_totals_applied"))
    detail = _hydrate_order_pricing(detail)

    customer = None
    if detail.get("customer_record_id"):
        raw_tags = detail.get("tags")
        if isinstance(raw_tags, str):
            raw_tags = json.loads(raw_tags or "[]")

        customer = {
            "id": str(detail["customer_record_id"]),
            "channel": detail.get("channel"),
            "platform_id": detail.get("platform_id"),
            "display_name": detail.get("display_name"),
            "phone": detail.get("phone"),
            "instagram_handle": detail.get("instagram_handle"),
            "tags": raw_tags or [],
            "total_orders": int(detail.get("total_orders") or 0),
            "total_spent": float(detail.get("total_spent") or 0),
            "first_contact": detail.get("first_contact"),
            "last_active": detail.get("last_active"),
            "notes": detail.get("notes"),
            "conversation_state": detail.get("conversation_state"),
            "last_shipping_address": detail.get("last_shipping_address"),
            "last_shipping_city": detail.get("last_shipping_city"),
            "last_shipping_method": detail.get("last_shipping_method"),
            "last_fulfillment_type": detail.get("last_fulfillment_type"),
            "last_shipping_zone": detail.get("last_shipping_zone"),
            "last_pickup_agency": detail.get("last_pickup_agency"),
        }

    recent_orders: list[dict] = []
    if customer:
        rows = await db.fetch_all(
            """
            SELECT id, total, payment_status, shipping_status, created_at
            FROM orders
            WHERE customer_id = :cid
            ORDER BY created_at DESC
            LIMIT 5
            """,
            {"cid": customer["id"]},
        )
        recent_orders = []
        for recent in rows:
            recent_dict = dict(recent)
            recent_orders.append({
                "id": str(recent_dict["id"]),
                "total": float(recent_dict.get("total") or 0),
                "payment_status": recent_dict.get("payment_status"),
                "shipping_status": recent_dict.get("shipping_status"),
                "created_at": recent_dict.get("created_at"),
            })

    for key in (
        "customer_record_id",
        "channel",
        "platform_id",
        "display_name",
        "phone",
        "instagram_handle",
        "tags",
        "total_orders",
        "total_spent",
        "first_contact",
        "last_active",
        "notes",
        "conversation_state",
        "last_shipping_address",
        "last_shipping_city",
        "last_shipping_method",
        "last_fulfillment_type",
        "last_shipping_zone",
        "last_pickup_agency",
    ):
        detail.pop(key, None)

    detail["customer"] = customer
    detail["recent_customer_orders"] = recent_orders
    return detail


async def get_latest_open_order(customer_id: str) -> dict | None:
    row = await db.fetch_one(
        """
        SELECT id
        FROM orders
        WHERE customer_id = :cid
          AND payment_status IN ('pending', 'proof_received')
          AND inventory_status IN ('reserved', 'legacy_unknown')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"cid": customer_id},
    )
    if not row:
        return None
    return await get_order(str(row["id"]))


async def get_active_unpaid_order_count(customer_id: str) -> int:
    """Return orders that still reserve inventory and require payment."""
    return await _get_active_unpaid_order_count(db, customer_id)


async def _get_active_unpaid_order_count(queryable, customer_id: str) -> int:
    row = await queryable.fetch_one(
        """
        SELECT COUNT(*) AS count
        FROM orders
        WHERE customer_id = :cid
          AND payment_status IN ('pending', 'proof_received')
          AND inventory_status IN ('reservation_pending', 'reserved', 'legacy_unknown')
        """,
        {"cid": customer_id},
    )
    return int(row["count"] or 0) if row else 0


async def get_latest_pending_order(customer_id: str) -> dict | None:
    """Return the latest order that still requires payment."""
    row = await db.fetch_one(
        """
        SELECT id
        FROM orders
        WHERE customer_id = :cid
          AND payment_status = 'pending'
          AND inventory_status IN ('reserved', 'legacy_unknown')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"cid": customer_id},
    )
    if not row:
        return None
    return await get_order(str(row["id"]))


async def get_unambiguous_open_order(customer_id: str) -> tuple[dict | None, bool]:
    """Return the sole open order, or flag ambiguity when multiple are open."""
    rows = await db.fetch_all(
        """
        SELECT id
        FROM orders
        WHERE customer_id = :cid
          AND payment_status IN ('pending', 'proof_received')
          AND inventory_status IN ('reserved', 'legacy_unknown')
        ORDER BY created_at DESC
        LIMIT 2
        """,
        {"cid": customer_id},
    )
    if len(rows) > 1:
        return None, True
    if not rows:
        return None, False
    return await get_order(str(rows[0]["id"])), False


async def get_customer_open_order_by_id(customer_id: str, order_id: str) -> dict | None:
    """Return an open order only when it belongs to the current customer."""
    row = await db.fetch_one(
        """
        SELECT id
        FROM orders
        WHERE id = :oid
          AND customer_id = :cid
          AND payment_status IN ('pending', 'proof_received')
          AND inventory_status IN ('reserved', 'legacy_unknown')
        LIMIT 1
        """,
        {"oid": order_id, "cid": customer_id},
    )
    if not row:
        return None
    return await get_order(str(row["id"]))


async def update_order_payment_status(
    order_id: str,
    status: str,
    note: str | None = None,
    proof_metadata: dict | None = None,
) -> dict | None:
    """
    Update an order payment status.
    Customer lifetime totals are applied exactly once, when the order first becomes paid.
    """
    if status not in VALID_PAYMENT_STATUSES:
        raise ValueError(f"Invalid payment status '{status}'")

    async with db.get_db().transaction():
        if proof_metadata:
            proof_hash = str(proof_metadata["proof_hash"])
            reference_key = str(proof_metadata["reference_key"])
            for lock_key in sorted((proof_hash, reference_key)):
                await db.fetch_one(
                    "SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))",
                    {"key": lock_key},
                )
            replay = await db.fetch_one(
                """
                SELECT id FROM orders
                WHERE id <> :oid
                  AND (payment_proof_hash = :proof_hash OR payment_reference_key = :reference_key)
                UNION ALL
                SELECT original_order_id AS id FROM payment_proof_replays
                WHERE proof_hash = :proof_hash OR reference_key = :reference_key
                LIMIT 1
                """,
                {
                    "oid": order_id,
                    "proof_hash": proof_hash,
                    "reference_key": reference_key,
                },
            )
            if replay:
                return {
                    "order_id": str(order_id),
                    "payment_status": "replay_detected",
                    "existing_order_id": str(replay["id"]),
                }

        row = await db.fetch_one(
            """
            SELECT id, customer_id, total, merchandise_total, payment_status, inventory_status, customer_totals_applied
            FROM orders
            WHERE id = :oid
            FOR UPDATE
            """,
            {"oid": order_id},
        )
        if not row:
            return None

        current_payment_status = str(row["payment_status"] or "")
        current_inventory_status = (
            str(row["inventory_status"] or "legacy_unknown")
            if "inventory_status" in row
            else "legacy_unknown"
        )
        if status in PAID_STATUSES and current_inventory_status not in {"reserved", "legacy_unknown"}:
            message = (
                f"Order {order_id} cannot be marked paid because its inventory state is "
                f"'{current_inventory_status}'."
            )
            if not proof_metadata:
                raise ValueError(message)
            logger.warning(message)
            return None
        if proof_metadata and (
            current_payment_status != "pending"
        ):
            logger.warning(
                "Payment proof update skipped for order %s because state changed to payment=%s inventory=%s",
                order_id,
                current_payment_status,
                current_inventory_status,
            )
            return None

        if proof_metadata:
            await db.execute(
                """
                UPDATE orders
                SET payment_status = :status,
                    payment_proof = COALESCE(:note, payment_proof),
                    payment_proof_hash = :proof_hash,
                    payment_reference = :reference,
                    payment_reference_key = :reference_key,
                    payment_currency = :currency,
                    payment_amount = :amount,
                    payment_transaction_at = :transaction_at,
                    payment_verified_at = NOW(),
                    updated_at = NOW()
                WHERE id = :oid
                """,
                {
                    "status": status,
                    "note": note,
                    "oid": order_id,
                    "proof_hash": proof_metadata["proof_hash"],
                    "reference": proof_metadata["reference"],
                    "reference_key": proof_metadata["reference_key"],
                    "currency": proof_metadata["currency"],
                    "amount": proof_metadata["amount"],
                    "transaction_at": proof_metadata["transaction_at"],
                },
            )
        else:
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

        totals_applied = bool(row["customer_totals_applied"])
        customer_id = str(row["customer_id"]) if row["customer_id"] is not None else None
        if (
            status in PAID_STATUSES
            and not totals_applied
            and customer_id is not None
        ):
            await _apply_paid_customer_updates(customer_id, _customer_spend_amount(row))
            await db.execute(
                """
                UPDATE orders
                SET customer_totals_applied = TRUE, updated_at = NOW()
                WHERE id = :oid
                """,
                {"oid": order_id},
            )
            totals_applied = True
        elif status not in PAID_STATUSES and totals_applied:
            if customer_id is not None:
                await _revert_paid_customer_updates(customer_id, _customer_spend_amount(row))
            await db.execute(
                """
                UPDATE orders
                SET customer_totals_applied = FALSE, updated_at = NOW()
                WHERE id = :oid
                """,
                {"oid": order_id},
            )
            totals_applied = False

    return {
        "order_id": str(order_id),
        "payment_status": status,
        "customer_totals_applied": totals_applied,
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
            tracking_number = CASE
                WHEN :update_tracking_number THEN :tracking_number
                ELSE tracking_number
            END,
            updated_at = NOW()
        WHERE id = :oid
        """,
        {
            "shipping_status": shipping_status,
            "tracking_number": (tracking_number or "").strip() or None,
            "update_tracking_number": tracking_number is not None,
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
    items: list[dict] = []
    restore_required = False

    async with _inventory_mutation_connection() as connection:
        async with connection.transaction():
            row = await connection.fetch_one(
                """
                SELECT id, items, inventory_status
                FROM orders
                WHERE id = :oid
                FOR UPDATE
                """,
                {"oid": order_id},
            )
            if not row:
                return None

            items = _load_items(row["items"])
            inventory_status = row["inventory_status"]
            if inventory_status == "reserved":
                restore_required = True
                await connection.execute(
                    """
                    UPDATE orders
                    SET inventory_status = :release_pending,
                        updated_at = NOW()
                    WHERE id = :oid
                    """,
                    {"oid": order_id, "release_pending": RELEASE_IN_PROGRESS},
                )
            elif inventory_status == RELEASE_IN_PROGRESS:
                restore_required = True

        if inventory_status == RESERVATION_IN_PROGRESS:
            restore_required = await _inventory_operation_applied(order_id, items, -1)
            if restore_required:
                async with connection.transaction():
                    await connection.execute(
                        """
                        UPDATE orders
                        SET inventory_status = :release_pending,
                            updated_at = NOW()
                        WHERE id = :oid
                          AND inventory_status = :reservation_pending
                        """,
                        {
                            "oid": order_id,
                            "release_pending": RELEASE_IN_PROGRESS,
                            "reservation_pending": RESERVATION_IN_PROGRESS,
                        },
                    )

        if restore_required:
            await _restore_order_inventory(order_id, items)

        async with connection.transaction():
            row = await connection.fetch_one(
                """
                SELECT id, customer_id, total, merchandise_total, customer_totals_applied,
                       payment_proof_hash, payment_reference_key
                FROM orders
                WHERE id = :oid
                FOR UPDATE
                """,
                {"oid": order_id},
            )
            if not row:
                return None

            if row["customer_totals_applied"] and row["customer_id"]:
                await _revert_paid_customer_updates(str(row["customer_id"]), _customer_spend_amount(row))

            try:
                proof_hash = row["payment_proof_hash"]
                reference_key = row["payment_reference_key"]
            except KeyError:
                proof_hash = None
                reference_key = None
            if proof_hash or reference_key:
                await connection.execute(
                    """
                    INSERT INTO payment_proof_replays (proof_hash, reference_key, original_order_id)
                    VALUES (:proof_hash, :reference_key, :order_id)
                    ON CONFLICT DO NOTHING
                    """,
                    {
                        "proof_hash": proof_hash,
                        "reference_key": reference_key,
                        "order_id": str(order_id),
                    },
                )

            await connection.execute(
                "DELETE FROM orders WHERE id = :oid",
                {"oid": order_id},
            )

    return {
        "order_id": str(order_id),
        "deleted": True,
        "restored_items": len(items) if restore_required else 0,
    }


async def release_expired_inventory_reservations(limit: int = 20) -> dict[str, int]:
    """Release unpaid inventory reservations older than the configured TTL."""
    rows = await db.fetch_all(
        """
        SELECT id
        FROM orders
        WHERE inventory_status IN ('reserved', :release_pending, :reservation_pending)
          AND (
              payment_status IN ('failed', 'rejected')
              OR (
                  payment_status = 'pending'
                  AND COALESCE(inventory_reserved_at, created_at) <= NOW() - (:ttl_hours * INTERVAL '1 hour')
              )
          )
        ORDER BY COALESCE(inventory_reserved_at, created_at) ASC
        LIMIT :limit
        """,
        {
            "limit": limit,
            "ttl_hours": INVENTORY_RESERVATION_TTL_HOURS,
            "release_pending": RELEASE_IN_PROGRESS,
            "reservation_pending": RESERVATION_IN_PROGRESS,
        },
    )
    released = 0
    failed = 0
    for row in rows:
        try:
            if await _release_inventory_reservation(str(row["id"])):
                released += 1
        except Exception:
            failed += 1
            logger.exception("Failed to release expired inventory reservation for order %s", row["id"])
    return {"checked": len(rows), "released": released, "failed": failed}


async def _release_inventory_reservation(order_id: str) -> bool:
    items: list[dict] = []
    restore_required = False
    releasable_statuses = {"pending", "failed", "rejected"}

    async with _inventory_mutation_connection() as connection:
        async with connection.transaction():
            row = await connection.fetch_one(
                """
                SELECT id, items, inventory_status, payment_status
                FROM orders
                WHERE id = :oid
                FOR UPDATE
                """,
                {"oid": order_id},
            )
            if not row or row["payment_status"] not in releasable_statuses:
                return False

            items = _load_items(row["items"])
            inventory_status = row["inventory_status"]
            if inventory_status == "reserved":
                restore_required = True
                await connection.execute(
                    """
                    UPDATE orders
                    SET inventory_status = :release_pending,
                        updated_at = NOW()
                    WHERE id = :oid
                    """,
                    {"oid": order_id, "release_pending": RELEASE_IN_PROGRESS},
                )
            elif inventory_status == RELEASE_IN_PROGRESS:
                restore_required = True
            elif inventory_status != RESERVATION_IN_PROGRESS:
                return False

        if inventory_status == RESERVATION_IN_PROGRESS:
            restore_required = await _inventory_operation_applied(order_id, items, -1)
            if restore_required:
                async with connection.transaction():
                    await connection.execute(
                        """
                        UPDATE orders
                        SET inventory_status = :release_pending,
                            updated_at = NOW()
                        WHERE id = :oid
                          AND inventory_status = :reservation_pending
                        """,
                        {
                            "oid": order_id,
                            "release_pending": RELEASE_IN_PROGRESS,
                            "reservation_pending": RESERVATION_IN_PROGRESS,
                        },
                    )

        if restore_required:
            await _restore_order_inventory(order_id, items)

        async with connection.transaction():
            row = await connection.fetch_one(
                """
                SELECT id, payment_status
                FROM orders
                WHERE id = :oid
                FOR UPDATE
                """,
                {"oid": order_id},
            )
            if not row or row["payment_status"] not in releasable_statuses:
                return False
            await connection.execute(
                """
                UPDATE orders
                SET inventory_status = :released,
                    inventory_released_at = NOW(),
                    payment_status = CASE WHEN payment_status = 'pending' THEN 'rejected' ELSE payment_status END,
                    updated_at = NOW()
                WHERE id = :oid
                """,
                {"oid": order_id, "released": RELEASED},
            )
    return True


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
