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
from app.ai.providers import AVAILABLE_MODELS, get_model_costs
from app.catalog.pdf_generator import generate_catalog_pdf, get_pdf_metadata, PDF_PATH
from app.catalog.sheets import get_cached_catalog
from app.channels.instagram_sender import setup_ice_breakers, subscribe_page_to_webhooks
from app.config import get_config
from app.admin.auth import require_admin
from app.admin.telegram_bot import setup_telegram_webhook
from app.crm.customers import add_tags, remove_tag

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


# LLM-related setting keys that are locked when managed from master
_LLM_MANAGED_KEYS = {
    "llm_provider", "llm_model", "llm_temperature", "llm_max_tokens",
    "fallback_provider", "fallback_model", "auto_fallback", "ab_test_enabled",
}


# ── Endpoints ────────────────────────────────────────────────

@router.get("/")
async def get_all_settings():
    """Return all current settings."""
    settings = await db.get_settings()
    config = get_config()
    settings["_llm_managed_externally"] = config.llm_managed_externally
    return settings


@router.get("/providers")
async def list_available_providers():
    """Return available providers and their models (for the admin dropdown)."""
    return AVAILABLE_MODELS


@router.put("/{key}")
async def update_setting(key: str, body: SettingUpdate):
    """
    Update a single setting by key.
    Validates provider/model combinations to prevent misconfigurations.
    """
    config = get_config()
    if config.llm_managed_externally and key in _LLM_MANAGED_KEYS:
        raise HTTPException(
            status_code=403,
            detail=f"LLM setting '{key}' is managed by the master admin. Contact your administrator.",
        )

    value = body.value

    # ── Validate provider changes ────────────────────────────
    if key in ("llm_provider", "fallback_provider"):
        if value not in VALID_PROVIDERS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid provider '{value}'. Choose from: {sorted(VALID_PROVIDERS)}",
            )

    # ── Validate model changes ───────────────────────────────
    if key in ("llm_model", "fallback_model"):
        if value not in VALID_MODELS_FLAT:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown model '{value}'. Available: {sorted(VALID_MODELS_FLAT)}",
            )
        # Check model belongs to the correct provider
        provider_key = "llm_provider" if key == "llm_model" else "fallback_provider"
        settings = await db.get_settings()
        provider = settings.get(provider_key, "openai")
        provider_model_ids = [m["id"] for m in AVAILABLE_MODELS.get(provider, [])]
        if value not in provider_model_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Model '{value}' is not available for provider '{provider}'. "
                       f"Available models: {provider_model_ids}",
            )

    # ── Validate temperature ─────────────────────────────────
    if key == "llm_temperature":
        temp = float(value)
        if not (0.0 <= temp <= 1.0):
            raise HTTPException(
                status_code=400,
                detail="Temperature must be between 0.0 and 1.0.",
            )
        value = temp

    # ── Validate max_tokens ──────────────────────────────────
    if key == "llm_max_tokens":
        tokens = int(value)
        if not (100 <= tokens <= 2000):
            raise HTTPException(
                status_code=400,
                detail="Max tokens must be between 100 and 2000.",
            )
        value = tokens

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

    logger.info(f"Setting updated: {key} = {value}")
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
    await setup_ice_breakers(ig_user_id)
    return {"status": "ok", "message": "Ice Breakers configured."}


@router.post("/instagram/subscribe-page")
async def subscribe_page_endpoint(page_id: str):
    """
    Subscribe the Facebook Page to messaging webhooks.
    Must be called once after initial setup to start receiving Instagram DMs.
    """
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
async def list_customers(tag: str | None = None, limit: int = 50):
    """Return recent customers, optionally filtered by tag."""
    if tag:
        rows = await db.fetch_all(
            """
            SELECT id, channel, platform_id, display_name, tags,
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
            SELECT id, channel, platform_id, display_name, tags,
                   total_orders, total_spent, conversation_state, last_active
            FROM customers
            ORDER BY last_active DESC NULLS LAST
            LIMIT :limit
            """,
            {"limit": limit},
        )
    return [dict(r) for r in rows]


@router.post("/customers/{customer_id}/resolve")
async def resolve_customer(customer_id: str):
    """Resolve an escalated customer back to active (AI resumes)."""
    row = await db.fetch_one(
        "SELECT id, display_name FROM customers WHERE id::text = :cid AND conversation_state = 'escalated'",
        {"cid": customer_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Customer not found or not escalated")

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

    pdf_path = generate_catalog_pdf(catalog)
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
