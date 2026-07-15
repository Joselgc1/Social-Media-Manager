"""
Server-owned checkout draft validation and finalization.
"""

from __future__ import annotations

from typing import Any

from app.admin.notify import notify_new_order
from app.ai.tools.catalog import find_catalog_matches, normalize_catalog_text
from app.catalog.sheets import get_cached_catalog, get_product_sizes
from app.crm import customers, orders, sessions
from app.payment_methods import payment_method_tag_value


async def update_checkout_draft(
    customer: dict,
    partial_update: dict,
    *,
    payment_methods: list[dict] | None = None,
) -> dict:
    """Validate and persist partial checkout fields without creating an order."""
    normalized_update = _normalize_update(customer, partial_update or {}, payment_methods or [])
    session = await sessions.update_checkout_draft(customer["id"], normalized_update)
    draft = session.checkout_draft
    errors = _validate_draft(draft)
    missing_fields = draft.missing_fields()

    return {
        "status": "ok" if not errors and not missing_fields else "needs_more_info",
        "draft": draft.public_dict(),
        "missing_fields": missing_fields,
        "errors": errors,
        "workflow_stage": session.workflow_stage,
    }


async def finalize_checkout(
    customer: dict,
    *,
    payment_methods: list[dict] | None = None,
    start_new_order: bool = False,
) -> dict:
    """Create an order from the server-side draft after deterministic validation."""
    customer_id = customer["id"]
    payment_methods = payment_methods or []
    session = await sessions.get_or_create_session(customer_id)

    if session.current_order_id:
        existing = await orders.get_order(session.current_order_id)
        if existing:
            return _finalized_response(existing, payment_methods, already_finalized=True)

    open_order = await orders.get_latest_open_order(customer_id)
    if open_order and not start_new_order:
        return {
            "status": "existing_unpaid_order",
            "message": (
                "Hay un pedido pendiente sin pago. Confirma si quieres continuar ese pedido "
                "o iniciar una compra separada."
            ),
            "order_id": open_order.get("id") or open_order.get("order_id"),
            "missing_fields": [],
        }

    draft = session.checkout_draft
    missing_fields = draft.missing_fields()
    if missing_fields:
        return {
            "status": "missing_fields",
            "draft": draft.public_dict(),
            "missing_fields": missing_fields,
            "errors": [],
        }

    errors = _validate_draft(draft)
    if errors:
        return {
            "status": "error",
            "draft": draft.public_dict(),
            "missing_fields": [],
            "errors": errors,
            "message": errors[0],
        }

    canonical_items = []
    for item in draft.items:
        resolved = _resolve_catalog_item(item)
        if not resolved["ok"]:
            return {
                "status": "error",
                "message": resolved["message"],
                "draft": draft.public_dict(),
                "missing_fields": [resolved.get("field", "items")],
            }
        product = resolved["product"]
        canonical_items.append({
            "product_name": product.get("product_name", ""),
            "sku": product.get("sku", ""),
            "size": item.size,
            "quantity": item.quantity,
            "unit_price": float(product.get("price_usd") or 0),
        })

    order = await orders.create_order(
        customer_id=customer_id,
        items=canonical_items,
        payment_method=draft.payment_method or "",
        shipping_city=draft.shipping_city,
        shipping_address=draft.shipping_address,
        shipping_method=draft.shipping_method,
    )

    if order.get("created_new", True):
        await _notify_checkout_order(customer, order, canonical_items)
    if draft.shipping_address:
        await _save_shipping_address(customer_id, draft)
    if draft.payment_method:
        await customers.add_tags(customer_id, [f"payment:{payment_method_tag_value(draft.payment_method)}"])

    await sessions.set_current_order(customer_id, order.get("order_id") or order.get("id"), workflow_stage="waiting_for_payment")
    return _finalized_response(order, payment_methods, already_finalized=not order.get("created_new", True))


async def cancel_checkout(customer_id: str, reason: str | None = None) -> dict:
    """Clear the active checkout workflow."""
    await sessions.reset_session(customer_id)
    return {
        "status": "cancelled",
        "message": reason or "Checkout cancelled and draft cleared.",
    }


def _normalize_update(customer: dict, update: dict[str, Any], payment_methods: list[dict]) -> dict:
    normalized = dict(update)
    if normalized.get("use_saved_address"):
        if customer.get("last_shipping_address"):
            normalized["shipping_address"] = customer.get("last_shipping_address")
        if customer.get("last_shipping_city"):
            normalized["shipping_city"] = customer.get("last_shipping_city")
        if customer.get("last_shipping_method"):
            normalized["shipping_method"] = customer.get("last_shipping_method")

    if "payment_method" in normalized:
        method = _find_payment_method_name(payment_methods, str(normalized.get("payment_method") or ""))
        normalized["payment_method"] = method or normalized.get("payment_method")

    if isinstance(normalized.get("items"), list):
        normalized["items"] = [_normalize_item_patch(item) for item in normalized["items"] if isinstance(item, dict)]
    else:
        normalized.update(_normalize_item_patch(normalized))
    return normalized


def _normalize_item_patch(item: dict) -> dict:
    clean = dict(item)
    clean.pop("unit_price", None)
    clean.pop("price", None)
    if clean.get("quantity") is not None:
        try:
            clean["quantity"] = max(int(clean["quantity"]), 1)
        except (TypeError, ValueError):
            clean.pop("quantity", None)
    if clean.get("size") is not None:
        clean["size"] = str(clean["size"]).strip().upper()

    candidate = sessions.CheckoutDraftItem.model_validate(clean)
    if candidate.product_query or candidate.sku or candidate.product_name or candidate.canonical_sku:
        resolved = _resolve_catalog_item(candidate, allow_missing_size=True)
        if resolved["ok"]:
            product = resolved["product"]
            clean["product_name"] = product.get("product_name")
            clean["sku"] = product.get("sku")
            clean["canonical_sku"] = product.get("sku")
            clean["parent_sku"] = product.get("parent_sku")
    return clean


def _validate_draft(draft: sessions.CheckoutDraft) -> list[str]:
    errors = []
    for item in draft.items:
        if not item.size:
            continue
        resolved = _resolve_catalog_item(item)
        if not resolved["ok"]:
            errors.append(resolved["message"])
    return errors


def _resolve_catalog_item(item: sessions.CheckoutDraftItem, *, allow_missing_size: bool = False) -> dict:
    query = item.canonical_sku or item.sku or item.product_query or item.product_name or ""
    size = (item.size or "").strip().upper()
    if not query:
        return {"ok": False, "message": "Falta el producto del pedido.", "field": "items.product"}
    if not size and not allow_missing_size:
        return {"ok": False, "message": "Falta la talla del producto.", "field": "items.size"}

    matches = find_catalog_matches(query, size_filter=size or None)
    if not matches:
        matches = _direct_catalog_matches(query, size)
    if not matches:
        return {"ok": False, "message": "No se encontró ese producto o talla en el catálogo actual.", "field": "items"}

    if size:
        sized_matches = [product for product in matches if size in get_product_sizes(product)]
        if sized_matches:
            matches = sized_matches
        else:
            return {"ok": False, "message": "Esa talla no está disponible para ese producto.", "field": "items.size"}

    in_stock_match = next((product for product in matches if _is_in_stock(product)), None)
    if not in_stock_match:
        return {"ok": False, "message": "Ese producto está agotado en este momento.", "field": "items"}
    if item.quantity and _available_stock(in_stock_match) is not None and item.quantity > _available_stock(in_stock_match):
        return {
            "ok": False,
            "message": "No hay suficientes unidades disponibles para esa cantidad.",
            "field": "items.quantity",
        }
    return {"ok": True, "product": in_stock_match}


def _direct_catalog_matches(query: str, size: str) -> list[dict]:
    normalized_query = normalize_catalog_text(query)
    matches = []
    for product in get_cached_catalog() or []:
        sku = normalize_catalog_text(product.get("sku", ""))
        parent_sku = normalize_catalog_text(product.get("parent_sku", ""))
        name = normalize_catalog_text(product.get("product_name", ""))
        if normalized_query not in {sku, parent_sku, name}:
            continue
        if size and size not in get_product_sizes(product):
            continue
        matches.append(product)
    return matches


def _is_in_stock(product: dict) -> bool:
    try:
        return float(product.get("stock") or 0) > 0
    except (TypeError, ValueError):
        return False


def _available_stock(product: dict) -> int | None:
    try:
        return int(float(product.get("stock")))
    except (TypeError, ValueError):
        return None


def _find_payment_method_name(payment_methods: list[dict], method_name: str) -> str | None:
    normalized = normalize_catalog_text(method_name)
    for payment_method in payment_methods:
        configured_name = str(payment_method.get("name", "") or "").strip()
        if normalize_catalog_text(configured_name) == normalized:
            return configured_name
    return None


def _payment_instructions(payment_methods: list[dict], method_name: str | None) -> str:
    if not method_name:
        return ""
    normalized = normalize_catalog_text(method_name)
    for payment_method in payment_methods:
        if normalize_catalog_text(payment_method.get("name", "")) == normalized:
            return str(payment_method.get("information", "") or "").strip()
    return ""


def _finalized_response(order: dict, payment_methods: list[dict], *, already_finalized: bool) -> dict:
    method_name = order.get("payment_method")
    return {
        "status": "already_finalized" if already_finalized else "created",
        "order_id": order.get("order_id") or order.get("id"),
        "items": order.get("items", []),
        "total": float(order.get("total") or 0),
        "subtotal": float(order.get("subtotal") or order.get("total") or 0),
        "discount_applied": bool(order.get("discount_applied")),
        "discount_amount": float(order.get("discount_amount") or 0),
        "payment_method": method_name,
        "payment_instructions": _payment_instructions(payment_methods, method_name),
        "workflow_stage": "waiting_for_payment",
    }


async def _notify_checkout_order(customer: dict, order: dict, items: list[dict]) -> None:
    items_summary = ", ".join(f"{item['product_name']} ({item['size']})" for item in items)
    await notify_new_order(
        customer_name=customer.get("display_name"),
        order_total=order["total"],
        payment_method=order["payment_method"],
        items_summary=items_summary,
    )


async def _save_shipping_address(customer_id: str, draft: sessions.CheckoutDraft) -> None:
    from app import db

    await db.execute(
        """UPDATE customers
           SET last_shipping_address = :addr,
               last_shipping_city = :city,
               last_shipping_method = :method
           WHERE id = :id""",
        {
            "addr": draft.shipping_address,
            "city": draft.shipping_city or "",
            "method": draft.shipping_method or "",
            "id": customer_id,
        },
    )
