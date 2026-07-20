"""
Admin settings API.
Lets the store owner change LLM provider, model, temperature,
and other configuration via HTTP endpoints or Telegram commands.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import db
from app.admin.auth import require_admin
from app.admin.telegram_bot import setup_telegram_webhook
from app.ai.providers import AVAILABLE_MODELS, get_model_costs
from app.catalog.pdf_generator import PDF_PATH, generate_catalog_pdf, get_pdf_metadata
from app.catalog.sheets import get_cached_catalog
from app.config import get_config
from app.crm import conversations, orders
from app.crm import customers as customer_crm
from app.crm.customers import add_tags, normalize_tags, remove_tag
from app.payment_methods import PAYMENT_METHODS_SETTING_KEY, normalize_payment_methods
from app.runtime_settings import LLM_MANAGED_KEYS, STORE_EDITABLE_SETTING_KEYS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/settings", tags=["admin"], dependencies=[Depends(require_admin)])

# Valid values for validation
VALID_PROVIDERS = set(AVAILABLE_MODELS.keys())
VALID_MODELS_FLAT = {
    model["id"]
    for models in AVAILABLE_MODELS.values()
    for model in models
}


class SettingUpdate(BaseModel):
    value: str | float | bool | int


class PaymentMethodItem(BaseModel):
    id: str | None = None
    name: str
    information: str


class PaymentMethodsUpdate(BaseModel):
    payment_methods: list[PaymentMethodItem]


class OrderUpdate(BaseModel):
    payment_status: str | None = None
    shipping_status: str | None = None
    tracking_number: str | None = None


class CustomerUpdate(BaseModel):
    channel: str | None = None
    conversation_state: str | None = None


VALID_CUSTOMER_CHANNELS = {"whatsapp", "instagram"}
VALID_CUSTOMER_STATES = {"active", "escalated", "blocked"}


def _coerce_setting_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    raise HTTPException(status_code=400, detail="Boolean setting must be true or false.")


def _validate_setting_value(key: str, value, current_settings: dict):
    if key not in STORE_EDITABLE_SETTING_KEYS:
        raise HTTPException(status_code=400, detail=f"Setting '{key}' is not editable.")

    if key in ("llm_provider", "fallback_provider"):
        if value not in VALID_PROVIDERS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid provider '{value}'. Choose from: {sorted(VALID_PROVIDERS)}",
            )
        return value

    if key in ("llm_model", "fallback_model"):
        if value not in VALID_MODELS_FLAT:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown model '{value}'. Available: {sorted(VALID_MODELS_FLAT)}",
            )
        provider_key = "llm_provider" if key == "llm_model" else "fallback_provider"
        provider = current_settings.get(provider_key, "openai")
        provider_model_ids = [m["id"] for m in AVAILABLE_MODELS.get(provider, [])]
        if value not in provider_model_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Model '{value}' is not available for provider '{provider}'. "
                       f"Available models: {provider_model_ids}",
            )
        return value

    if key == "llm_temperature":
        temp = float(value)
        if not (0.0 <= temp <= 1.0):
            raise HTTPException(
                status_code=400,
                detail="Temperature must be between 0.0 and 1.0.",
            )
        return temp

    if key == "llm_max_tokens":
        tokens = int(value)
        if not (100 <= tokens <= 2000):
            raise HTTPException(
                status_code=400,
                detail="Max tokens must be between 100 and 2000.",
            )
        return tokens

    if key == "max_conversation_history":
        history = int(value)
        if not (5 <= history <= 50):
            raise HTTPException(
                status_code=400,
                detail="Conversation history must be between 5 and 50.",
            )
        return history

    if key == "catalog_pdf_interval_hours":
        hours = int(value)
        if not (1 <= hours <= 168):
            raise HTTPException(
                status_code=400,
                detail="Catalog PDF interval must be between 1 and 168 hours.",
            )
        return hours

    if key in ("auto_fallback", "ai_enabled", "escalation_telegram_enabled", "kommo_strip_emoji"):
        return _coerce_setting_bool(value)

    if key == "catalog_refresh_minutes":
        minutes = int(value)
        if not (1 <= minutes <= 1440):
            raise HTTPException(
                status_code=400,
                detail="Catalog refresh must be between 1 and 1440 minutes.",
            )
        return minutes

    if key == "order_discount_percent":
        percent = float(value)
        if not (0.0 <= percent <= 100.0):
            raise HTTPException(
                status_code=400,
                detail="Discount percent must be between 0 and 100.",
            )
        return round(percent, 2)

    if key == "order_discount_threshold_usd":
        threshold = float(value)
        if not (0.0 <= threshold <= 100000.0):
            raise HTTPException(
                status_code=400,
                detail="Discount threshold must be between 0 and 100000 USD.",
            )
        return round(threshold, 2)

    return value


# ── Endpoints ────────────────────────────────────────────────

@router.get("/")
async def get_all_settings():
    """Return all current settings."""
    settings = dict(await db.get_settings())
    config = get_config()
    settings["_llm_managed_externally"] = config.llm_managed_externally
    return settings


@router.get("/payment-methods")
async def get_payment_methods():
    settings = dict(await db.get_settings())
    raw_methods = settings.get(PAYMENT_METHODS_SETTING_KEY, [])
    try:
        payment_methods = normalize_payment_methods(raw_methods)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=f"Stored payment methods are invalid: {e}") from e
    return {"payment_methods": payment_methods}


@router.get("/providers")
async def list_available_providers():
    """Return available providers and their models (for the admin dropdown)."""
    return AVAILABLE_MODELS


@router.get("/kommo/status")
async def kommo_status():
    """Safe Kommo configuration and job diagnostics."""
    config = get_config()
    try:
        from app.integrations.kommo.jobs import diagnostics_summary

        diagnostics = await diagnostics_summary()
    except Exception as e:
        logger.warning("Kommo diagnostics summary unavailable: %s", e)
        diagnostics = {
            "pending_job_count": 0,
            "failed_job_count": 0,
            "stale_job_count": 0,
            "last_kommo_api_error_summary": "Diagnostics unavailable",
        }
    return {
        "channel_backend": config.channel_backend,
        "kommo_subdomain_configured": bool(config.kommo_subdomain),
        "kommo_access_token_configured": bool(config.kommo_access_token),
        "kommo_integration_id_configured": bool(config.kommo_integration_id),
        "kommo_integration_secret_configured": bool(config.kommo_integration_secret),
        "kommo_salesbot_id_configured": config.kommo_salesbot_id is not None,
        "kommo_webhook_secret_configured": bool(config.kommo_webhook_secret),
        "kommo_ai_mode_field_configured": config.kommo_ai_mode_field_id is not None,
        "kommo_ai_mode_enum_ids_configured": all(
            value is not None
            for value in (
                config.kommo_ai_active_enum_id,
                config.kommo_ai_human_enum_id,
                config.kommo_ai_paused_enum_id,
            )
        ),
        "kommo_responsible_user_configured": config.kommo_default_responsible_user_id is not None,
        **diagnostics,
    }


@router.post("/kommo/test")
async def kommo_test():
    """Run safe read-only Kommo connectivity and configuration checks."""
    config = get_config()
    if config.channel_backend != "kommo":
        return {"channel_backend": config.channel_backend, "checks": [], "ok": False}

    from app.integrations.kommo.client import KommoClient, sanitize_kommo_error

    client = KommoClient.from_config()
    checks = []

    async def _check(name: str, func):
        try:
            await func()
            checks.append({"name": name, "ok": True})
        except Exception as e:
            checks.append({"name": name, "ok": False, "error": sanitize_kommo_error(e)})

    await _check("account_connectivity", client.get_account)

    async def _field_check():
        field = await client.get_lead_custom_field(config.kommo_ai_mode_field_id)
        enums = {
            int(enum.get("id"))
            for enum in (field.get("enums") or [])
            if str(enum.get("id", "")).isdigit()
        }
        expected = {
            int(config.kommo_ai_active_enum_id),
            int(config.kommo_ai_human_enum_id),
            int(config.kommo_ai_paused_enum_id),
        }
        if not expected.issubset(enums):
            raise RuntimeError("AI Mode enum IDs were not found on the configured field")

    await _check("ai_mode_field_and_enums", _field_check)

    if config.kommo_default_responsible_user_id:
        await _check(
            "responsible_user",
            lambda: client.get_user(config.kommo_default_responsible_user_id),
        )

    salesbot_id_ok = isinstance(config.kommo_salesbot_id, int) and config.kommo_salesbot_id > 0
    checks.append({"name": "salesbot_id_format", "ok": salesbot_id_ok})

    return {"channel_backend": config.channel_backend, "ok": all(item["ok"] for item in checks), "checks": checks}


@router.put("/payment-methods")
async def update_payment_methods(body: PaymentMethodsUpdate):
    try:
        payment_methods = normalize_payment_methods(
            [item.model_dump() for item in body.payment_methods]
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    await db.execute(
        """
        INSERT INTO settings (key, value)
        VALUES (:key, :val)
        ON CONFLICT (key) DO UPDATE SET value = :val, updated_at = NOW()
        """,
        {
            "key": PAYMENT_METHODS_SETTING_KEY,
            "val": json.dumps(payment_methods, ensure_ascii=False),
        },
    )
    db.invalidate_settings_cache()
    logger.info("Payment methods updated.")
    return {"status": "updated", "payment_methods": payment_methods}


@router.put("/{key}")
async def update_setting(key: str, body: SettingUpdate):
    """
    Update a single setting by key.
    Validates provider/model combinations to prevent misconfigurations.
    """
    config = get_config()
    if config.llm_managed_externally and key in LLM_MANAGED_KEYS:
        raise HTTPException(
            status_code=403,
            detail=f"LLM setting '{key}' is managed by the master admin. Contact your administrator.",
        )

    settings = await db.get_settings()
    value = _validate_setting_value(key, body.value, settings)

    # ── Write to database ────────────────────────────────────
    existing = await db.fetch_one(
        "SELECT key FROM settings WHERE key = :key", {"key": key}
    )

    if existing:
        await db.execute(
            "UPDATE settings SET value = :val, updated_at = NOW() WHERE key = :key",
            {"val": json.dumps(value), "key": key},
        )
    else:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (:key, :val)",
            {"key": key, "val": json.dumps(value)},
        )

    # Bust the cache so the change takes effect immediately
    db.invalidate_settings_cache()

    logger.info(f"Setting updated: {key}")
    return {"key": key, "value": value, "status": "updated"}


@router.post("/switch-provider")
async def quick_switch_provider(provider: str, model: str | None = None):
    """
    Quick endpoint to switch the active LLM provider and optionally the model.
    If no model is specified, uses the provider's default model.

    Example: POST /admin/settings/switch-provider?provider=anthropic
    """
    config = get_config()
    if config.llm_managed_externally:
        raise HTTPException(
            status_code=403,
            detail="LLM provider switching is managed by the master admin. Contact your administrator.",
        )

    if provider not in VALID_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid provider '{provider}'. Choose from: {sorted(VALID_PROVIDERS)}",
        )

    # Find the default model for this provider if none specified
    if model is None:
        for m in AVAILABLE_MODELS.get(provider, []):
            if m.get("default"):
                model = m["id"]
                break
        if model is None:
            model = AVAILABLE_MODELS[provider][0]["id"]

    # Validate model belongs to provider
    provider_model_ids = [m["id"] for m in AVAILABLE_MODELS.get(provider, [])]
    if model not in provider_model_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Model '{model}' is not available for '{provider}'. "
                   f"Available: {provider_model_ids}",
        )

    # Update both settings
    await db.execute(
        "UPDATE settings SET value = :val, updated_at = NOW() WHERE key = 'llm_provider'",
        {"val": json.dumps(provider)},
    )
    await db.execute(
        "UPDATE settings SET value = :val, updated_at = NOW() WHERE key = 'llm_model'",
        {"val": json.dumps(model)},
    )

    db.invalidate_settings_cache()

    logger.info(f"Provider switched to {provider}/{model}")
    return {
        "provider": provider,
        "model": model,
        "status": "switched",
    }


@router.get("/usage-summary")
async def usage_summary(days: int = 1):
    """
    Return token usage and estimated cost broken down by provider/model.
    Use ?days=7 or ?days=30 for multi-day ranges.
    """
    from datetime import date, timedelta
    days = max(1, min(days, 90))
    cutoff = date.today() - timedelta(days=days - 1)

    rows = await db.fetch_all(
        """
        SELECT provider, model,
               COUNT(*) as calls,
               SUM(input_tokens) as total_input,
               SUM(output_tokens) as total_output
        FROM usage_log
        WHERE created_at >= :cutoff
        GROUP BY provider, model
        ORDER BY provider, model
        """,
        {"cutoff": cutoff},
    )

    summary = []
    total_cost = 0.0

    for row in rows:
        model = row["model"]
        rates = get_model_costs(model)
        input_cost = (row["total_input"] / 1_000_000) * rates["input"]
        output_cost = (row["total_output"] / 1_000_000) * rates["output"]
        row_cost = input_cost + output_cost
        total_cost += row_cost

        summary.append({
            "provider": row["provider"],
            "model": model,
            "calls": row["calls"],
            "input_tokens": row["total_input"],
            "output_tokens": row["total_output"],
            "estimated_cost_usd": round(row_cost, 4),
        })

    return {
        "days": days,
        "breakdown": summary,
        "total_estimated_cost_usd": round(total_cost, 4),
    }


# ── Instagram setup endpoints ────────────────────────────────

@router.post("/instagram/setup-ice-breakers")
async def setup_ice_breakers_endpoint(ig_user_id: str):
    """
    Configure default Ice Breakers for the Instagram account.
    These are the FAQ prompts that appear when a customer opens your DM
    for the first time.

    Parameters
    ----------
    ig_user_id : Your Instagram Professional account's numeric user ID.
    """
    from app.channels.instagram_sender import setup_ice_breakers

    await setup_ice_breakers(ig_user_id)
    return {"status": "ok", "message": "Ice Breakers configured."}


@router.post("/instagram/subscribe-page")
async def subscribe_page_endpoint(page_id: str):
    """
    Subscribe the Facebook Page to messaging webhooks.
    Must be called once after initial setup to start receiving Instagram DMs.
    """
    from app.channels.instagram_sender import subscribe_page_to_webhooks

    await subscribe_page_to_webhooks(page_id)
    return {"status": "ok", "message": f"Page {page_id} subscribed to messaging webhooks."}


@router.get("/stats/conversations")
async def conversation_stats(days: int = 1):
    """
    Return conversation and order stats broken down by channel.
    Use ?days=7 or ?days=30 for multi-day ranges.
    """
    from datetime import date, timedelta
    days = max(1, min(days, 90))
    cutoff = date.today() - timedelta(days=days - 1)

    rows = await db.fetch_all(
        """
        SELECT
            c.channel,
            COUNT(DISTINCT c.customer_id) as unique_customers,
            COUNT(*) as total_messages,
            COUNT(*) FILTER (WHERE c.role = 'user') as customer_messages,
            COUNT(*) FILTER (WHERE c.role = 'assistant') as bot_messages
        FROM conversations c
        WHERE c.created_at >= :cutoff
        GROUP BY c.channel
        """,
        {"cutoff": cutoff},
    )

    channels = {}
    for row in rows:
        channels[row["channel"]] = {
            "unique_customers": row["unique_customers"],
            "total_messages": row["total_messages"],
            "customer_messages": row["customer_messages"],
            "bot_messages": row["bot_messages"],
        }

    order_rows = await db.fetch_all(
        """
        SELECT
            COUNT(*) as total_orders,
            COUNT(*) FILTER (WHERE payment_status = 'pending') as pending_payment,
            COUNT(*) FILTER (WHERE payment_status = 'proof_received') as proof_received,
            COUNT(*) FILTER (WHERE payment_status = 'confirmed') as confirmed,
            COALESCE(SUM(total), 0) as total_revenue
        FROM orders
        WHERE created_at >= :cutoff
        """,
        {"cutoff": cutoff},
    )

    order_data = dict(order_rows[0]) if order_rows else {}

    return {
        "days": days,
        "conversations": channels,
        "orders": order_data,
    }


@router.get("/customers")
async def list_customers(tag: str | None = None, limit: int = 200):
    """Return recent customers, optionally filtered by tag."""
    limit = max(1, min(limit, 500))
    if tag:
        rows = await db.fetch_all(
            """
            SELECT id, channel, platform_id, display_name, phone, instagram_handle, tags,
                   total_orders, total_spent, conversation_state, last_active
            FROM customers
            WHERE tags::text LIKE :pattern
            ORDER BY last_active DESC NULLS LAST
            LIMIT :limit
            """,
            {"pattern": f"%{tag}%", "limit": limit},
        )
    else:
        rows = await db.fetch_all(
            """
            SELECT id, channel, platform_id, display_name, phone, instagram_handle, tags,
                   total_orders, total_spent, conversation_state, last_active
            FROM customers
            ORDER BY last_active DESC NULLS LAST
            LIMIT :limit
            """,
            {"limit": limit},
        )
    customers = []
    for row in rows:
        item = dict(row)
        raw_tags = item.get("tags")
        parsed_tags = raw_tags if isinstance(raw_tags, list) else json.loads(raw_tags or "[]")
        item["tags"] = normalize_tags(parsed_tags)
        customers.append(item)
    return customers


@router.post("/customers/{customer_id}/resolve")
async def resolve_customer(customer_id: str):
    """Resolve an escalated customer back to active (AI resumes)."""
    row = await db.fetch_one(
        "SELECT id, display_name FROM customers WHERE id::text = :cid AND conversation_state = 'escalated'",
        {"cid": customer_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Customer not found or not escalated")

    await conversations.clear_history(str(row["id"]))
    await db.execute(
        "UPDATE customers SET conversation_state = 'active' WHERE id = :id",
        {"id": row["id"]},
    )
    return {"status": "resolved", "customer_id": str(row["id"])}


@router.post("/customers/resolve-all")
async def resolve_all_customers():
    """Resolve all escalated customers back to active."""
    result = await db.fetch_one(
        "SELECT COUNT(*) as cnt FROM customers WHERE conversation_state = 'escalated'"
    )
    count = result["cnt"] if result else 0

    if count == 0:
        return {"status": "ok", "resolved": 0}

    rows = await db.fetch_all(
        "SELECT id FROM customers WHERE conversation_state = 'escalated'"
    )
    await conversations.clear_history_for_customers([str(row["id"]) for row in rows])
    await db.execute(
        "UPDATE customers SET conversation_state = 'active' WHERE conversation_state = 'escalated'"
    )
    return {"status": "resolved", "resolved": count}


class TagsPayload(BaseModel):
    tags: list[str]


@router.get("/customers/{customer_id}/tags")
async def get_customer_tags(customer_id: str):
    """Get all tags for a customer."""
    row = await db.fetch_one(
        "SELECT id, display_name, platform_id, tags FROM customers WHERE id::text = :cid",
        {"cid": customer_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Customer not found")

    tags = row["tags"] if isinstance(row["tags"], list) else json.loads(row["tags"] or "[]")
    tags = normalize_tags(tags)
    return {"customer_id": customer_id, "tags": tags}


@router.post("/customers/{customer_id}/tags")
async def add_customer_tags(customer_id: str, body: TagsPayload):
    """Add tags to a customer."""
    row = await db.fetch_one(
        "SELECT id FROM customers WHERE id::text = :cid",
        {"cid": customer_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Customer not found")

    await add_tags(customer_id, body.tags)
    return {"status": "ok", "added": body.tags}


@router.delete("/customers/{customer_id}/tags/{tag}")
async def delete_customer_tag(customer_id: str, tag: str):
    """Remove a tag from a customer."""
    row = await db.fetch_one(
        "SELECT id FROM customers WHERE id::text = :cid",
        {"cid": customer_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Customer not found")

    await remove_tag(customer_id, tag)
    return {"status": "ok", "removed": tag}


@router.get("/orders")
async def list_orders(limit: int = 50):
    """Return recent orders with customer info."""
    rows = await db.fetch_all(
        """
        SELECT o.id, o.items, o.total, o.payment_method, o.payment_status,
               o.shipping_method, o.shipping_city, o.shipping_address,
               o.shipping_status, o.tracking_number, o.created_at,
               c.display_name, c.platform_id, c.channel
        FROM orders o
        JOIN customers c ON o.customer_id = c.id
        ORDER BY o.created_at DESC
        LIMIT :limit
        """,
        {"limit": limit},
    )
    return [dict(r) for r in rows]


@router.get("/orders/{order_id}")
async def get_order_detail(order_id: str):
    detail = await orders.get_order_detail(order_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Order not found")
    return detail


@router.put("/customers/{customer_id}")
async def update_customer(customer_id: str, body: CustomerUpdate):
    row = await db.fetch_one(
        """
        SELECT id, channel, platform_id, conversation_state
        FROM customers
        WHERE id::text = :cid
        """,
        {"cid": customer_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Customer not found")

    updates: dict[str, str] = {}

    if body.channel is not None:
        channel = body.channel.strip().lower()
        if channel not in VALID_CUSTOMER_CHANNELS:
            raise HTTPException(status_code=400, detail="Invalid channel")
        if channel != row["channel"]:
            conflict = await db.fetch_one(
                """
                SELECT id
                FROM customers
                WHERE channel = :channel
                  AND platform_id = :platform_id
                  AND id <> :id
                LIMIT 1
                """,
                {
                    "channel": channel,
                    "platform_id": row["platform_id"],
                    "id": row["id"],
                },
            )
            if conflict:
                raise HTTPException(
                    status_code=400,
                    detail="Another customer already exists with that channel and platform ID.",
                )
            updates["channel"] = channel

    if body.conversation_state is not None:
        state = body.conversation_state.strip().lower()
        if state not in VALID_CUSTOMER_STATES:
            raise HTTPException(status_code=400, detail="Invalid conversation state")
        updates["conversation_state"] = state

    if not updates:
        existing = await db.fetch_one("SELECT * FROM customers WHERE id::text = :cid", {"cid": customer_id})
        return {"status": "unchanged", "customer": dict(existing) if existing else None}

    if updates.get("conversation_state") == "active" and row["conversation_state"] != "active":
        await conversations.clear_history(str(row["id"]))

    updated = await customer_crm.update_customer(
        customer_id=str(row["id"]),
        channel=updates.get("channel"),
        conversation_state=updates.get("conversation_state"),
    )
    return {"status": "updated", "customer": updated}


@router.delete("/customers/{customer_id}")
async def delete_customer(customer_id: str):
    deleted = await customer_crm.delete_customer(customer_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Customer not found")
    return {"status": "deleted", "customer_id": customer_id}


@router.put("/orders/{order_id}")
async def update_order(order_id: str, body: OrderUpdate):
    existing = await orders.get_order(order_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Order not found")

    updated_payment = None
    updated_shipping = None

    try:
        if body.payment_status is not None:
            updated_payment = await orders.update_order_payment_status(order_id, body.payment_status)
        if body.shipping_status is not None or body.tracking_number is not None:
            updated_shipping = await orders.update_order_shipping(
                order_id,
                shipping_status=body.shipping_status,
                tracking_number=body.tracking_number,
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    updated = await orders.get_order(order_id)
    return {
        "status": "updated",
        "order": updated,
        "payment": updated_payment,
        "shipping": updated_shipping,
    }


@router.delete("/orders/{order_id}")
async def delete_order(order_id: str):
    result = await orders.delete_order(order_id)
    if not result:
        raise HTTPException(status_code=404, detail="Order not found")
    return result


@router.post("/telegram/setup-webhook")
async def setup_telegram_webhook_endpoint():
    """
    Register the Telegram webhook URL so the admin bot receives commands.
    Call this once after deployment.
    """
    config = get_config()
    webhook_url = f"{config.app_base_url}/webhooks/telegram"

    result = await setup_telegram_webhook(config.telegram_bot_token, webhook_url)
    return {"webhook_url": webhook_url, "telegram_response": result}


# ── Catalog PDF endpoints ─────────────────────────────────────

@router.post("/catalog/generate-pdf")
async def generate_catalog_pdf_endpoint():
    """
    Trigger on-demand PDF catalog generation.
    Uses the current cached product catalog from Google Sheets.
    """
    catalog = get_cached_catalog()
    if not catalog:
        raise HTTPException(status_code=400, detail="Catalog is empty. Check Google Sheets connection.")

    generate_catalog_pdf(catalog)
    if get_config().channel_backend == "kommo":
        from app.integrations.kommo.files import sync_catalog_pdf_to_kommo

        await sync_catalog_pdf_to_kommo(PDF_PATH)
    meta = get_pdf_metadata()

    return {
        "status": "generated",
        "product_count": meta.get("product_count", 0),
        "generated_at": meta.get("generated_at"),
        "pdf_url": "/static/catalog/catalog.pdf",
    }


@router.get("/catalog/pdf-status")
async def catalog_pdf_status():
    """Return the current status of the catalog PDF (last generated, product count)."""
    meta = get_pdf_metadata()
    return {
        "exists": PDF_PATH.exists(),
        "generated_at": meta.get("generated_at"),
        "product_count": meta.get("product_count", 0),
        "pdf_url": "/static/catalog/catalog.pdf" if PDF_PATH.exists() else None,
    }


@router.get("/catalog/download-pdf")
async def download_catalog_pdf():
    """Download the current catalog PDF."""
    if not PDF_PATH.exists():
        raise HTTPException(status_code=404, detail="Catalog PDF not generated yet. Use /generate-pdf first.")
    return FileResponse(
        path=str(PDF_PATH),
        media_type="application/pdf",
        filename="catalogo_vs.pdf",
    )
