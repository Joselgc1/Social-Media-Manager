"""
Server-owned checkout draft validation and finalization.
"""

from __future__ import annotations

from typing import Any

from app import db
from app.admin.notify import notify_new_order
from app.ai.tools.catalog import find_catalog_matches, normalize_catalog_text
from app.catalog.sheets import get_cached_catalog, get_product_sizes
from app.crm import customers, orders, sessions
from app.payment_methods import payment_method_tag_value
from app.shipping import DEFAULT_SHIPPING_POLICY, normalize_shipping_policy, resolve_delivery_quote


async def update_checkout_draft(
    customer: dict,
    partial_update: dict,
    *,
    payment_methods: list[dict] | None = None,
) -> dict:
    """Validate and persist partial checkout fields without creating an order."""
    policy = await _get_shipping_policy()
    normalized_update = _normalize_update(customer, partial_update or {}, payment_methods or [])
    normalized_update = _apply_delivery_context(normalized_update, policy)
    session = await sessions.update_checkout_draft(customer["id"], normalized_update)
    draft = session.checkout_draft
    quote = _quote_for_draft(draft, policy)
    errors = _validate_draft(draft, quote)
    missing_fields = _missing_fields(draft, quote)
    workflow_stage = session.workflow_stage
    if not errors and not missing_fields and workflow_stage != "checkout_ready":
        session = await sessions.set_workflow_stage(customer["id"], "checkout_ready")
        workflow_stage = session.workflow_stage

    return {
        "status": "ok" if not errors and not missing_fields else "needs_more_info",
        "draft": draft.public_dict(),
        "missing_fields": missing_fields,
        "errors": errors,
        "delivery_quote": _public_quote(quote),
        "workflow_stage": workflow_stage,
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
    policy = await _get_shipping_policy()
    session = await sessions.get_or_create_session(customer_id)

    if session.current_order_id and session.workflow_stage == "waiting_for_payment" and not start_new_order:
        existing = await orders.get_order(session.current_order_id)
        if existing:
            return _finalized_response(existing, payment_methods, already_finalized=True)

    open_order = await orders.get_latest_pending_order(customer_id)
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

    if start_new_order:
        pending_order_count = await orders.get_active_unpaid_order_count(customer_id)
        if pending_order_count >= orders.MAX_ACTIVE_UNPAID_ORDERS:
            return _pending_order_limit_response(pending_order_count)

    draft = session.checkout_draft
    quote = _quote_for_draft(draft, policy)
    missing_fields = _missing_fields(draft, quote)
    if missing_fields:
        return {
            "status": "missing_fields",
            "draft": draft.public_dict(),
            "missing_fields": missing_fields,
            "errors": [],
            "delivery_quote": _public_quote(quote),
        }

    errors = _validate_draft(draft, quote)
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
        canonical_items.append(_canonical_order_item(item, resolved["product"]))

    try:
        order = await orders.create_order(
            customer_id=customer_id,
            items=canonical_items,
            payment_method=draft.payment_method or "",
            shipping_city=quote.get("shipping_city") or draft.shipping_city,
            shipping_address=draft.shipping_address if quote.get("fulfillment_type") == "home_delivery" else None,
            shipping_method=quote.get("shipping_method"),
            fulfillment_type=quote.get("fulfillment_type"),
            shipping_zone=quote.get("shipping_zone"),
            pickup_agency=draft.pickup_agency if quote.get("fulfillment_type") == "courier_agency_pickup" else None,
            shipping_fee=quote.get("shipping_fee", 0),
            shipping_currency=quote.get("shipping_currency", "USD"),
        )
    except orders.PendingOrderLimitError as exc:
        return _pending_order_limit_response(exc.pending_order_count)

    if order.get("created_new", True):
        await _notify_checkout_order(customer, order, canonical_items)
    if quote.get("fulfillment_type"):
        await _save_shipping_address(customer_id, draft, quote)
    if draft.payment_method:
        await customers.add_tags(customer_id, [f"payment:{payment_method_tag_value(draft.payment_method)}"])

    await sessions.set_current_order(
        customer_id,
        order.get("order_id") or order.get("id"),
        workflow_stage="waiting_for_payment",
    )
    return _finalized_response(order, payment_methods, already_finalized=not order.get("created_new", True))


async def cancel_checkout(customer_id: str, reason: str | None = None) -> dict:
    """Clear the active checkout workflow."""
    await sessions.reset_session(customer_id)
    return {
        "status": "cancelled",
        "message": reason or "Checkout cancelled and draft cleared.",
    }


async def validate_legacy_delivery(args: dict) -> dict:
    """Validate delivery and canonicalize products for the legacy direct-order tool."""
    draft = sessions.CheckoutDraft.model_validate({
        "items": args.get("items") or [],
        "shipping_city": args.get("shipping_city"),
        "shipping_address": args.get("shipping_address"),
        "shipping_method": args.get("shipping_method"),
        "shipping_zone": args.get("shipping_zone"),
        "pickup_agency": args.get("pickup_agency"),
        "payment_method": args.get("payment_method"),
    })
    quote = _quote_for_draft(draft, await _get_shipping_policy())
    missing_fields = _missing_fields(draft, quote)
    errors = _validate_draft(draft, quote)
    if missing_fields or errors or quote.get("status") != "quoted":
        message = (errors or [quote.get("message") or "Faltan datos de entrega o producto."])[0]
        return {
            "status": "error",
            "message": message,
            "missing_fields": missing_fields,
            "delivery_quote": _public_quote(quote),
        }

    canonical_items = []
    for item in draft.items:
        resolved = _resolve_catalog_item(item)
        if not resolved["ok"]:
            return {
                "status": "error",
                "message": resolved["message"],
                "missing_fields": [resolved.get("field", "items")],
                "delivery_quote": _public_quote(quote),
            }
        canonical_items.append(_canonical_order_item(item, resolved["product"]))
    return {"status": "ok", "quote": quote, "items": canonical_items}


def _normalize_update(customer: dict, update: dict[str, Any], payment_methods: list[dict]) -> dict:
    normalized = dict(update)
    if normalized.get("use_saved_address"):
        fulfillment_type = customer.get("last_fulfillment_type")
        if fulfillment_type:
            normalized["fulfillment_type"] = fulfillment_type
        if customer.get("last_shipping_address"):
            normalized["shipping_address"] = customer.get("last_shipping_address")
        if customer.get("last_shipping_city"):
            normalized["shipping_city"] = customer.get("last_shipping_city")
        if customer.get("last_shipping_method"):
            normalized["shipping_method"] = customer.get("last_shipping_method")
        if customer.get("last_shipping_zone"):
            normalized["shipping_zone"] = customer.get("last_shipping_zone")
        if customer.get("last_pickup_agency"):
            normalized["pickup_agency"] = customer.get("last_pickup_agency")

    if "payment_method" in normalized:
        method = _find_payment_method_name(payment_methods, str(normalized.get("payment_method") or ""))
        normalized["payment_method"] = method or normalized.get("payment_method")

    if isinstance(normalized.get("items"), list):
        normalized["items"] = [
            _normalize_item_patch(item)
            for item in normalized["items"]
            if isinstance(item, dict)
        ]
    else:
        normalized.update(_normalize_item_patch(normalized))
    return normalized


def _apply_delivery_context(update: dict, policy: dict) -> dict:
    """Persist only delivery attributes derived from the configured policy."""
    normalized = dict(update)
    if "shipping_city" not in normalized:
        return normalized

    quote = resolve_delivery_quote(
        policy,
        city=normalized.get("shipping_city"),
        delivery_zone=normalized.get("shipping_zone"),
        shipping_method=normalized.get("shipping_method"),
    )
    fulfillment_type = quote.get("fulfillment_type")
    if not fulfillment_type:
        return normalized

    normalized["fulfillment_type"] = fulfillment_type
    if quote.get("shipping_city"):
        normalized["shipping_city"] = quote["shipping_city"]
    if fulfillment_type == "home_delivery":
        normalized["shipping_method"] = None
        normalized["pickup_agency"] = None
        if quote.get("shipping_zone"):
            normalized["shipping_zone"] = quote["shipping_zone"]
    else:
        normalized["shipping_address"] = None
        normalized["shipping_zone"] = None
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
        clean["size"] = _normalize_variant_value(clean["size"])

    candidate = sessions.CheckoutDraftItem.model_validate(clean)
    if candidate.product_query or candidate.sku or candidate.product_name or candidate.canonical_sku:
        resolved = _resolve_catalog_item(candidate, allow_missing_size=True)
        if resolved["ok"]:
            product = resolved["product"]
            clean["product_name"] = product.get("product_name")
            clean["sku"] = product.get("sku")
            clean["canonical_sku"] = product.get("sku")
            clean["parent_sku"] = product.get("parent_sku")
            product_sizes = get_product_sizes(product)
            if not clean.get("size") and len(product_sizes) == 1:
                clean["size"] = product_sizes[0]
    return clean


def _validate_draft(draft: sessions.CheckoutDraft, quote: dict | None = None) -> list[str]:
    errors = []
    for item in draft.items:
        resolved = _resolve_catalog_item(item, allow_missing_size=True)
        if not resolved["ok"] and not resolved.get("needs_presentation"):
            errors.append(resolved["message"])
    if quote and quote.get("status") == "rate_unavailable":
        errors.append(quote["message"])
    return errors


def _missing_fields(draft: sessions.CheckoutDraft, quote: dict) -> list[str]:
    missing = []
    for field in draft.missing_fields():
        if field.endswith(".size"):
            continue
        if field not in {"shipping_method", "shipping_address", "shipping_zone", "pickup_agency"}:
            missing.append(field)

    for index, item in enumerate(draft.items):
        if item.size or not (item.canonical_sku or item.sku or item.product_query or item.product_name):
            continue
        resolved = _resolve_catalog_item(item, allow_missing_size=True)
        if resolved.get("needs_presentation"):
            missing.append(f"items[{index}].size")

    fulfillment_type = quote.get("fulfillment_type") or draft.fulfillment_type
    if fulfillment_type == "home_delivery":
        if not draft.shipping_zone:
            missing.append("shipping_zone")
        if not draft.shipping_address:
            missing.append("shipping_address")
    elif fulfillment_type == "courier_agency_pickup":
        if not draft.shipping_method:
            missing.append("shipping_method")
        if not draft.pickup_agency:
            missing.append("pickup_agency")
    else:
        if not draft.shipping_method:
            missing.append("shipping_method")
        if not draft.shipping_address:
            missing.append("shipping_address")
    return list(dict.fromkeys(missing))


async def _get_shipping_policy() -> dict:
    try:
        settings = await db.get_settings()
    except RuntimeError:
        return normalize_shipping_policy(DEFAULT_SHIPPING_POLICY)
    return normalize_shipping_policy(settings.get("shipping_policy"))


def _quote_for_draft(draft: sessions.CheckoutDraft, policy: dict) -> dict:
    return resolve_delivery_quote(
        policy,
        city=draft.shipping_city,
        delivery_zone=draft.shipping_zone,
        shipping_method=draft.shipping_method,
    )


def _public_quote(quote: dict) -> dict:
    return {
        key: quote[key]
        for key in (
            "status",
            "fulfillment_type",
            "shipping_city",
            "shipping_zone",
            "shipping_method",
            "shipping_fee",
            "shipping_currency",
            "available_zones",
            "message",
        )
        if key in quote
    }


def _resolve_catalog_item(item: sessions.CheckoutDraftItem, *, allow_missing_size: bool = False) -> dict:
    del allow_missing_size  # Kept for call-site compatibility; missing size is resolved catalog-aware below.
    query = item.canonical_sku or item.sku or item.product_query or item.product_name or ""
    size = _normalize_variant_value(item.size)
    if not query:
        return {"ok": False, "message": "Falta el producto del pedido.", "field": "items.product"}

    matches = find_catalog_matches(query, size_filter=size or None)
    if not matches:
        matches = _direct_catalog_matches(query, size)
    if not matches:
        return {
            "ok": False,
            "message": "No se encontró ese producto o presentación en el catálogo actual.",
            "field": "items",
        }

    if size:
        sized_matches = [product for product in matches if size in get_product_sizes(product)]
        if sized_matches:
            matches = sized_matches
        else:
            return {
                "ok": False,
                "message": "Esa presentación u opción no está disponible para ese producto.",
                "field": "items.size",
            }

    in_stock_matches = [product for product in matches if _is_in_stock(product)]
    if not in_stock_matches:
        return {"ok": False, "message": "Ese producto está agotado en este momento.", "field": "items"}

    if not size and len(in_stock_matches) > 1:
        presentations = sorted({
            value
            for product in in_stock_matches
            for value in get_product_sizes(product)
            if value
        })
        distinct_skus = {str(product.get("sku") or "") for product in in_stock_matches}
        if len(presentations) > 1 or len(distinct_skus) > 1:
            return {
                "ok": False,
                "needs_presentation": True,
                "message": "Ese producto tiene varias presentaciones u opciones; falta elegir una.",
                "field": "items.size",
                "presentations": presentations,
            }

    product = in_stock_matches[0]
    if item.quantity and _available_stock(product) is not None and item.quantity > _available_stock(product):
        return {
            "ok": False,
            "message": "No hay suficientes unidades disponibles para esa cantidad.",
            "field": "items.quantity",
        }
    return {"ok": True, "product": product}


def _direct_catalog_matches(query: str, size: str) -> list[dict]:
    normalized_query = normalize_catalog_text(query)
    matches = []
    for product in get_cached_catalog() or []:
        sku = normalize_catalog_text(product.get("sku", ""))
        parent_sku = normalize_catalog_text(product.get("parent_sku", ""))
        name = normalize_catalog_text(product.get("product_name", ""))
        brand = normalize_catalog_text(product.get("brand", ""))
        brand_name = " ".join(part for part in (brand, name) if part)
        if normalized_query not in {sku, parent_sku, name, brand_name}:
            continue
        if size and size not in get_product_sizes(product):
            continue
        matches.append(product)
    return matches


def _canonical_order_item(item: sessions.CheckoutDraftItem, product: dict) -> dict:
    product_sizes = get_product_sizes(product)
    presentation = _normalize_variant_value(item.size) or (
        product_sizes[0] if len(product_sizes) == 1 else ""
    )
    return {
        "product_name": product.get("product_name", ""),
        "sku": product.get("sku", ""),
        "size": presentation,
        "quantity": item.quantity,
        "unit_price": float(product.get("price_usd") or 0),
    }


def _normalize_variant_value(value) -> str:
    return " ".join(str(value or "").strip().upper().split())


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
        "merchandise_total": float(order.get("merchandise_total") or order.get("total") or 0),
        "shipping_fee": float(order.get("shipping_fee") or 0),
        "shipping_currency": order.get("shipping_currency") or "USD",
        "fulfillment_type": order.get("fulfillment_type"),
        "shipping_city": order.get("shipping_city"),
        "shipping_zone": order.get("shipping_zone"),
        "shipping_method": order.get("shipping_method"),
        "pickup_agency": order.get("pickup_agency"),
        "payment_method": method_name,
        "payment_instructions": _payment_instructions(payment_methods, method_name),
        "workflow_stage": "waiting_for_payment",
    }


def _pending_order_limit_response(pending_order_count: int) -> dict:
    return {
        "status": "pending_order_limit_reached",
        "message": (
            f"Tienes {pending_order_count} pedidos pendientes de pago. "
            "Paga o envía el comprobante de alguno antes de crear otro pedido."
        ),
        "pending_order_count": pending_order_count,
        "missing_fields": [],
    }


async def _notify_checkout_order(customer: dict, order: dict, items: list[dict]) -> None:
    items_summary = ", ".join(
        f"{item['product_name']} ({item['size']})" if item.get("size") else item["product_name"]
        for item in items
    )
    await notify_new_order(
        customer_name=customer.get("display_name"),
        order_total=order["total"],
        payment_method=order["payment_method"],
        items_summary=items_summary,
        delivery_summary=_delivery_summary(order),
    )


def _delivery_summary(order: dict) -> str:
    fee = float(order.get("shipping_fee") or 0)
    if order.get("fulfillment_type") == "home_delivery":
        location = ", ".join(
            value
            for value in (order.get("shipping_city"), order.get("shipping_zone"))
            if value
        )
        return f"Domicilio {location or 'Metro Valencia'} (${fee:.2f})"
    agency = order.get("pickup_agency") or "agencia por confirmar"
    courier = str(order.get("shipping_method") or "").upper()
    return f"Retiro {courier} en {agency} (${fee:.2f})"


async def _save_shipping_address(customer_id: str, draft: sessions.CheckoutDraft, quote: dict) -> None:
    await db.execute(
        """UPDATE customers
           SET last_shipping_address = :addr,
               last_shipping_city = :city,
               last_shipping_method = :method,
               last_fulfillment_type = :fulfillment_type,
               last_shipping_zone = :zone,
               last_pickup_agency = :pickup_agency
           WHERE id = :id""",
        {
            "addr": draft.shipping_address if quote.get("fulfillment_type") == "home_delivery" else None,
            "city": quote.get("shipping_city") or draft.shipping_city or "",
            "method": quote.get("shipping_method") or "",
            "fulfillment_type": quote.get("fulfillment_type"),
            "zone": quote.get("shipping_zone"),
            "pickup_agency": (
                draft.pickup_agency
                if quote.get("fulfillment_type") == "courier_agency_pickup"
                else None
            ),
            "id": customer_id,
        },
    )
