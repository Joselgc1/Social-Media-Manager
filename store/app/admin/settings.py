"""
Admin settings API.
Lets the store owner change LLM provider, model, temperature,
and other configuration via HTTP endpoints or Telegram commands.
"""

import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import db
from app.admin.auth import require_admin
from app.admin.customer_activation import ManualActivationError, activate_customer_for_admin
from app.admin.telegram_bot import setup_telegram_webhook
from app.ai.providers import AVAILABLE_MODELS, get_model_costs, list_providers
from app.catalog.pdf_generator import (
    PDF_PATH,
    ensure_catalog_pdf,
    generate_catalog_pdf,
    get_pdf_metadata,
    is_catalog_pdf_current,
)
from app.catalog.sheets import get_cached_catalog
from app.config import get_config
from app.crm import customers as customer_crm
from app.crm import escalations, orders
from app.crm.customers import add_tags, normalize_tags, remove_tag
from app.exchange_rates import ALLOWED_EXCHANGE_RATE_REFERENCES, normalize_rate_setting_value
from app.payment_methods import PAYMENT_METHODS_SETTING_KEY, normalize_payment_methods
from app.runtime_settings import LLM_MANAGED_KEYS, STORE_EDITABLE_SETTING_KEYS
from app.shipping import normalize_shipping_policy

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/settings", tags=["admin"], dependencies=[Depends(require_admin)])

# Valid values for validation
VALID_PROVIDERS = set(AVAILABLE_MODELS.keys())
VALID_MODELS_FLAT = {
    model["id"]
    for models in AVAILABLE_MODELS.values()
    for model in models
}
VALID_ORCHESTRATION_MODES = {"legacy", "shadow", "multi_agent"}


class SettingUpdate(BaseModel):
    value: str | float | bool | int


class SettingsBatchUpdate(BaseModel):
    settings: dict[str, str | float | bool | int]


class PaymentMethodItem(BaseModel):
    id: str | None = None
    name: str
    information: str


class PaymentMethodsUpdate(BaseModel):
    payment_methods: list[PaymentMethodItem]


class ShippingPolicyUpdate(BaseModel):
    shipping_policy: dict


class OrderUpdate(BaseModel):
    payment_status: str | None = None
    shipping_status: str | None = None
    tracking_number: str | None = None


class CustomerUpdate(BaseModel):
    channel: str | None = None
    conversation_state: str | None = None
    marketing_opt_in: bool | None = None


VALID_CUSTOMER_CHANNELS = {"whatsapp", "instagram"}
VALID_CUSTOMER_STATES = {"active", "escalated", "blocked"}
VALID_KOMMO_EMOJI_MODES = {"preserve", "safe", "strip"}
_PHONE_ALLOWED_RE = re.compile(r"^[+0-9 ().-]*$")


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


def _normalize_store_phone_number(value) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    if len(text) > 40 or not _PHONE_ALLOWED_RE.fullmatch(text) or not any(ch.isdigit() for ch in text):
        raise HTTPException(
            status_code=400,
            detail="Store phone number must be a valid public WhatsApp number.",
        )
    return text


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

    if key == "automatic_escalation_timeout_minutes":
        minutes = int(value)
        if minutes == 0:
            return 0
        if not (5 <= minutes <= 10080):
            raise HTTPException(
                status_code=400,
                detail="Automatic escalation timeout must be 0 or between 5 and 10080 minutes.",
            )
        return minutes

    if key == "ai_orchestration_mode":
        mode = str(value or "").strip().lower()
        if mode not in VALID_ORCHESTRATION_MODES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid orchestration mode '{value}'. Choose from: {sorted(VALID_ORCHESTRATION_MODES)}",
            )
        return mode

    if key == "exchange_rate_reference":
        reference = str(value or "").strip().lower()
        if reference not in ALLOWED_EXCHANGE_RATE_REFERENCES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid exchange rate reference '{value}'. Choose from: {sorted(ALLOWED_EXCHANGE_RATE_REFERENCES)}",
            )
        return reference

    if key == "manual_exchange_rate":
        try:
            return normalize_rate_setting_value(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if key == "store_phone_number":
        return _normalize_store_phone_number(value)

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

    if key in ("kommo_emoji_mode_whatsapp", "kommo_emoji_mode_instagram"):
        mode = str(value).strip().lower()
        if mode not in VALID_KOMMO_EMOJI_MODES:
            raise HTTPException(status_code=400, detail="Kommo emoji mode must be preserve, safe, or strip.")
        return mode

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


FALLBACK_KEYS = {"auto_fallback", "fallback_provider", "fallback_model"}


def _validate_auto_fallback_configuration(current_settings: dict) -> None:
    """Reject auto_fallback=True that silently references a provider without an API key.

    Availability reflects the runtime-initialized provider instances. When none are
    initialized yet (e.g. unit tests that never called init_providers) the check is
    skipped so local/test flows are not spuriously rejected.
    """
    if not current_settings.get("auto_fallback"):
        return
    fallback_provider = current_settings.get("fallback_provider", "anthropic")
    available = set(list_providers())
    if available and fallback_provider not in available:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Fallback provider '{fallback_provider}' is not available "
                f"(no API key configured). Available providers: {sorted(available)}. "
                "Disable auto_fallback or pick an available fallback provider."
            ),
        )


# ── Endpoints ────────────────────────────────────────────────

async def _apply_settings_batch(raw_settings: dict) -> dict:
    if not raw_settings:
        raise HTTPException(status_code=400, detail="At least one setting is required.")

    config = get_config()
    managed_keys = sorted(set(raw_settings) & LLM_MANAGED_KEYS)
    if config.llm_managed_externally and managed_keys:
        raise HTTPException(
            status_code=403,
            detail=f"LLM settings are managed by the master admin: {', '.join(managed_keys)}.",
        )

    current_settings = dict(await db.get_settings())
    validated = {}
    ordered_keys = [
        key for key in ("llm_provider", "fallback_provider")
        if key in raw_settings
    ]
    ordered_keys.extend(key for key in raw_settings if key not in ordered_keys)
    for key in ordered_keys:
        try:
            value = _validate_setting_value(key, raw_settings[key], current_settings)
        except HTTPException:
            raise
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"Invalid value for setting '{key}'.") from e
        validated[key] = value
        current_settings[key] = value

    if FALLBACK_KEYS.intersection(raw_settings):
        _validate_auto_fallback_configuration(current_settings)

    async with db.get_db().transaction():
        await db.execute(
            """
            INSERT INTO settings (key, value)
            SELECT item.key, item.value
            FROM jsonb_each(CAST(:updates AS jsonb)) AS item(key, value)
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated_at = NOW()
            """,
            {"updates": json.dumps(validated, ensure_ascii=False)},
        )

    db.invalidate_settings_cache()
    logger.info("Settings updated atomically: %s", sorted(validated))
    return validated

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


@router.get("/shipping-policy")
async def get_shipping_policy():
    settings = dict(await db.get_settings())
    try:
        shipping_policy = normalize_shipping_policy(settings.get("shipping_policy"))
    except ValueError as e:
        raise HTTPException(status_code=500, detail=f"Stored shipping policy is invalid: {e}") from e
    return {"shipping_policy": shipping_policy}


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
    try:
        from app.integrations.kommo.delivery import monthly_usage_summary

        media_usage = await monthly_usage_summary(config.kommo_chats_api_monthly_limit)
    except Exception as e:
        logger.warning("Kommo media usage diagnostics unavailable: %s", e)
        media_usage = {
            "attempted_requests": 0,
            "product_image_requests": 0,
            "catalog_pdf_requests": 0,
            "accepted_or_confirmed_deliveries": 0,
            "failed_deliveries": 0,
            "delivery_unknown_deliveries": 0,
            "configured_monthly_limit": config.kommo_chats_api_monthly_limit,
            "estimated_remaining_requests": None,
            "utilization_percent": None,
            "warning_level": "normal",
            "diagnostics_available": False,
        }
    return {
        "channel_backend": config.channel_backend,
        "kommo_subdomain_configured": bool(config.kommo_subdomain),
        "kommo_access_token_configured": bool(config.kommo_access_token),
        "kommo_integration_id_configured": bool(config.kommo_integration_id),
        "kommo_integration_secret_configured": bool(config.kommo_integration_secret),
        "kommo_salesbot_id": config.kommo_salesbot_id,
        "kommo_salesbot_id_configured": config.kommo_salesbot_id is not None,
        "kommo_instagram_dm_salesbot_id": config.kommo_instagram_dm_salesbot_id,
        "kommo_instagram_dm_salesbot_id_configured": (
            config.kommo_instagram_dm_salesbot_id or config.kommo_salesbot_id
        ) is not None,
        "kommo_whatsapp_salesbot_id": config.kommo_whatsapp_salesbot_id,
        "kommo_whatsapp_salesbot_id_configured": (
            config.kommo_whatsapp_salesbot_id or config.kommo_salesbot_id
        ) is not None,
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
        "kommo_chats_media_enabled": config.kommo_chats_media_enabled,
        "kommo_chats_product_images_enabled": (
            config.kommo_chats_media_enabled and config.kommo_chats_product_images_enabled
        ),
        "kommo_chats_catalog_pdf_enabled": (
            config.kommo_chats_media_enabled and config.kommo_chats_catalog_pdf_enabled
        ),
        "kommo_chats_pdf_attachment_type_configured": (
            config.kommo_chats_pdf_attachment_type is not None
        ),
        "kommo_chats_api_monthly_usage": media_usage,
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

    for channel, salesbot_id in (
        ("instagram_dm", config.kommo_instagram_dm_salesbot_id or config.kommo_salesbot_id),
        ("whatsapp", config.kommo_whatsapp_salesbot_id or config.kommo_salesbot_id),
    ):
        salesbot_id_ok = isinstance(salesbot_id, int) and salesbot_id > 0
        checks.append({"name": f"{channel}_salesbot_id_format", "ok": salesbot_id_ok})

    return {"channel_backend": config.channel_backend, "ok": all(item["ok"] for item in checks), "checks": checks}


@router.get("/meta-instagram-context/status")
async def meta_instagram_context_status():
    """Safe context-provider diagnostics without payloads or credentials."""
    config = get_config()
    try:
        from app.integrations.meta_context.service import diagnostics_summary

        diagnostics = await diagnostics_summary()
    except Exception:
        logger.exception("Meta Instagram context diagnostics unavailable")
        diagnostics = {
            "last_meta_event": None,
            "pending_event_count": 0,
            "matched_event_count": 0,
            "ambiguous_event_count": 0,
            "timed_out_kommo_job_count": 0,
            "story_events_received": 0,
            "story_events_matched": 0,
            "story_events_ambiguous": 0,
            "story_events_expired": 0,
            "story_correlation_timeouts": 0,
            "story_mapping_resolved": 0,
            "story_mapping_missing": 0,
            "story_context_created": 0,
            "story_context_reused": 0,
            "story_context_expired": 0,
            "receipt_level_text_matches": 0,
            "last_meta_api_error": "Diagnostics unavailable",
        }
    return {
        "enabled": config.meta_instagram_context_enabled,
        "story_enabled": config.meta_story_context_enabled,
        "channel_backend": config.channel_backend,
        "meta_app_secret_configured": bool(config.meta_app_secret),
        "instagram_access_token_configured": bool(config.instagram_access_token),
        "instagram_verify_token_configured": bool(config.instagram_verify_token),
        "instagram_account_id_configured": bool(config.instagram_account_id),
        "graph_api_version": config.meta_graph_api_version,
        "context_wait_seconds": config.meta_context_wait_seconds,
        "match_window_seconds": config.meta_context_match_window_seconds,
        "event_retention_hours": config.meta_context_event_retention_hours,
        "story_context_wait_seconds": config.meta_story_context_wait_seconds,
        "story_match_window_seconds": config.meta_story_context_match_window_seconds,
        "story_mapping_ttl_hours": config.instagram_story_mapping_ttl_hours,
        "story_context_ttl_hours": config.instagram_story_context_ttl_hours,
        **diagnostics,
    }


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


@router.put("/batch")
async def update_settings_batch(body: SettingsBatchUpdate):
    """Validate and update a related group of settings atomically."""
    validated = await _apply_settings_batch(body.settings)
    return {"status": "updated", "settings": validated}

@router.put("/shipping-policy")
async def update_shipping_policy(body: ShippingPolicyUpdate):
    try:
        shipping_policy = normalize_shipping_policy(body.shipping_policy)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    await db.execute(
        """
        INSERT INTO settings (key, value)
        VALUES (:key, :val)
        ON CONFLICT (key) DO UPDATE SET value = :val, updated_at = NOW()
        """,
        {"key": "shipping_policy", "val": json.dumps(shipping_policy, ensure_ascii=False)},
    )
    db.invalidate_settings_cache()
    logger.info("Shipping policy updated.")
    return {"status": "updated", "shipping_policy": shipping_policy}


@router.put("/{key}")
async def update_setting(key: str, body: SettingUpdate):
    """
    Update a single setting by key.
    Validates provider/model combinations to prevent misconfigurations.
    """
    value = (await _apply_settings_batch({key: body.value}))[key]
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

    await _apply_settings_batch({"llm_provider": provider, "llm_model": model})

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
                    total_orders, total_spent, conversation_state, marketing_opt_in,
                    marketing_opt_in_at, marketing_opt_out_at, last_active
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
                    total_orders, total_spent, conversation_state, marketing_opt_in,
                    marketing_opt_in_at, marketing_opt_out_at, last_active
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
        """
        SELECT id, display_name, channel, platform_id, conversation_state
        FROM customers
        WHERE id::text = :cid AND conversation_state = 'escalated'
        """,
        {"cid": customer_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Customer not found or not escalated")

    try:
        result = await activate_customer_for_admin(dict(row))
    except ManualActivationError as e:
        raise HTTPException(status_code=502, detail=e.safe_detail) from e

    return {"status": "resolved", "customer_id": result.customer_id, "activation_status": result.status}


@router.post("/customers/resolve-all")
async def resolve_all_customers():
    """Resolve all escalated customers back to active."""
    rows = await db.fetch_all(
        """
        SELECT id, display_name, channel, platform_id, conversation_state
        FROM customers
        WHERE conversation_state = 'escalated'
        """
    )
    if not rows:
        return {"status": "ok", "resolved": 0, "failed": 0, "results": []}

    results = []
    for row in rows:
        customer_id = str(row["id"])
        try:
            result = await activate_customer_for_admin(dict(row))
            results.append({"customer_id": result.customer_id, "status": result.status})
        except ManualActivationError as e:
            results.append({"customer_id": customer_id, "status": "failed", "detail": e.safe_detail})

    resolved = sum(1 for result in results if result["status"] in {"activated", "local_only"})
    failed = sum(1 for result in results if result["status"] == "failed")
    status = "resolved" if failed == 0 else "partial" if resolved else "failed"
    return {"status": status, "resolved": resolved, "failed": failed, "results": results}


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
        SELECT o.id, o.items, o.total, o.merchandise_total, o.shipping_fee, o.shipping_currency,
               o.payment_method, o.payment_status, o.fulfillment_type, o.shipping_method,
               o.shipping_city, o.shipping_address, o.shipping_zone, o.pickup_agency,
               o.shipping_status, o.tracking_number, o.created_at,
               CASE WHEN c.id IS NULL THEN 'Cliente eliminado' ELSE c.display_name END AS display_name,
               c.platform_id, c.channel
        FROM orders o
        LEFT JOIN customers c ON o.customer_id = c.id
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

    updates: dict[str, str | bool] = {}

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

    if body.marketing_opt_in is not None:
        updates["marketing_opt_in"] = body.marketing_opt_in

    if not updates:
        existing = await db.fetch_one("SELECT * FROM customers WHERE id::text = :cid", {"cid": customer_id})
        return {"status": "unchanged", "customer": dict(existing) if existing else None}

    if updates.get("conversation_state") == "active" and row["conversation_state"] != "active":
        try:
            result = await activate_customer_for_admin(dict(row), channel=updates.get("channel"))
        except ManualActivationError as e:
            raise HTTPException(status_code=502, detail=e.safe_detail) from e
        customer = result.customer
        if body.marketing_opt_in is not None:
            customer = await customer_crm.update_customer(
                customer_id=str(row["id"]),
                marketing_opt_in=body.marketing_opt_in,
            )
        return {"status": "updated", "customer": customer, "activation_status": result.status}

    requested_state = updates.get("conversation_state")
    if requested_state == "escalated":
        updated = await escalations.escalate_customer_manually(str(row["id"]), channel=updates.get("channel"))
    elif requested_state == "blocked":
        updated = await escalations.mark_customer_blocked(str(row["id"]), channel=updates.get("channel"))
    else:
        updated = await customer_crm.update_customer(
            customer_id=str(row["id"]),
            channel=updates.get("channel"),
            conversation_state=requested_state,
            marketing_opt_in=updates.get("marketing_opt_in"),
        )
    if requested_state in {"escalated", "blocked"} and body.marketing_opt_in is not None:
        updated = await customer_crm.update_customer(
            customer_id=str(row["id"]),
            marketing_opt_in=body.marketing_opt_in,
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
        if body.payment_status is not None and body.payment_status not in orders.VALID_PAYMENT_STATUSES:
            raise ValueError(f"Invalid payment status '{body.payment_status}'")
        if body.shipping_status is not None and body.shipping_status not in orders.VALID_SHIPPING_STATUSES:
            raise ValueError(f"Invalid shipping status '{body.shipping_status}'")
        async with db.get_db().transaction():
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
    if not (
        config.telegram_bot_token
        and config.telegram_admin_chat_id
        and config.telegram_webhook_secret
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_CHAT_ID, and "
                "TELEGRAM_WEBHOOK_SECRET must all be configured."
            ),
        )
    webhook_url = f"{config.app_base_url}/webhooks/telegram"

    result = await setup_telegram_webhook(
        config.telegram_bot_token,
        webhook_url,
        config.telegram_webhook_secret,
    )
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
    catalog = get_cached_catalog()
    current = is_catalog_pdf_current(catalog) if catalog else False
    return {
        "exists": current,
        "generated_at": meta.get("generated_at"),
        "product_count": meta.get("product_count", 0),
        "pdf_url": "/static/catalog/catalog.pdf" if current else None,
    }


@router.get("/catalog/download-pdf")
async def download_catalog_pdf():
    """Download the current catalog PDF."""
    catalog = get_cached_catalog()
    if not catalog:
        raise HTTPException(status_code=404, detail="Catalog is empty. Check Google Sheets connection.")
    try:
        ensure_catalog_pdf(catalog)
    except Exception as e:
        logger.error("Could not prepare catalog PDF download: %s", e)
        raise HTTPException(status_code=500, detail="Could not generate catalog PDF.") from e
    return FileResponse(
        path=str(PDF_PATH),
        media_type="application/pdf",
        filename="catalogo_vs.pdf",
    )
