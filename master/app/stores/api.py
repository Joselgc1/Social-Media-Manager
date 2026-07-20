"""
CRUD API for managing stores and their credentials.
"""

import asyncio
import json
import logging
import time
from contextlib import suppress
from datetime import date, timedelta

import databases as db_lib
from fastapi import APIRouter, Depends, HTTPException, Request

from app import db
from app.auth import require_auth
from app.config import get_config
from app.stores import exchange_rates as exchange_rate_service
from app.stores.crypto import decrypt, encrypt, mask
from app.stores.models import CredentialSet, LLMSettingsUpdate, RuntimeSettingsUpdate, StoreCreate, StoreUpdate
from app.stores.runtime_settings import (
    DEFAULT_RUNTIME_SETTINGS,
    MASTER_EDITABLE_RUNTIME_SETTING_KEYS,
    PROVIDER_EXCHANGE_RATE_SETTING_KEYS,
    SYNCABLE_RUNTIME_SETTING_KEYS,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stores", tags=["stores"], dependencies=[Depends(require_auth)])

BLOCKED_PAYMENT_SETTING_KEYS = {
    "payment_methods",
    "payment_zelle_details",
    "payment_binance_details",
    "payment_zinli_details",
    "payment_bolivares_details",
}

# Serialize cross-DB stats queries so we do not burst past Supabase Session pooler limits
# when the dashboard loads many stores in parallel (and store apps already hold pool slots).
_store_stats_semaphore: asyncio.Semaphore | None = None


def _stats_semaphore() -> asyncio.Semaphore:
    global _store_stats_semaphore
    if _store_stats_semaphore is None:
        n = max(1, get_config().store_stats_max_concurrent)
        _store_stats_semaphore = asyncio.Semaphore(n)
    return _store_stats_semaphore


# ── Store DB connection cache ──────────────────────────────
# Reuse connections to store databases instead of creating/destroying per request.
# Entries expire after _POOL_TTL_SECONDS of inactivity.
_POOL_TTL_SECONDS = 120  # close idle connections after 2 minutes
_store_pools: dict[str, tuple[db_lib.Database, float]] = {}  # url -> (db, last_used)
_pool_lock = asyncio.Lock()


async def _get_store_db(store_db_url: str) -> db_lib.Database:
    """Get or create a cached connection pool for a store database."""
    async with _pool_lock:
        if store_db_url in _store_pools:
            pool, _ = _store_pools[store_db_url]
            _store_pools[store_db_url] = (pool, time.monotonic())
            return pool
        pool = db_lib.Database(store_db_url, min_size=1, max_size=3)
        await pool.connect()
        _store_pools[store_db_url] = (pool, time.monotonic())
        return pool


async def cleanup_idle_pools():
    """Disconnect store DB pools that have been idle for too long."""
    async with _pool_lock:
        now = time.monotonic()
        expired = [url for url, (_, ts) in _store_pools.items() if now - ts > _POOL_TTL_SECONDS]
        for url in expired:
            pool, _ = _store_pools.pop(url)
            with suppress(Exception):
                await pool.disconnect()


# ── Audit helper ─────────────────────────────────────────────

async def _audit(action: str, store_id: str | None = None, detail: str = ""):
    await db.execute(
        "INSERT INTO master_audit_log (action, store_id, detail) VALUES (:action, :store_id, :detail)",
        {"action": action, "store_id": store_id, "detail": detail},
    )


# ── Store CRUD ───────────────────────────────────────────────

@router.get("/")
async def list_stores():
    """List all registered stores."""
    rows = await db.fetch_all(
        "SELECT id, name, owner_name, owner_contact, app_url, status, created_at, last_seen "
        "FROM stores ORDER BY created_at"
    )
    return [dict(row._mapping) for row in rows]


@router.get("/{store_id}")
async def get_store(store_id: str):
    """Get a single store's details."""
    row = await db.fetch_one("SELECT * FROM stores WHERE id = :id", {"id": store_id})
    if not row:
        raise HTTPException(status_code=404, detail="Store not found")
    result = dict(row._mapping)
    # Never return the encrypted DB URL raw — mask it
    if result.get("db_url_encrypted"):
        try:
            decrypted = decrypt(result["db_url_encrypted"])
            result["db_url_masked"] = mask(decrypted, 20)
        except Exception:
            result["db_url_masked"] = "****"
    del result["db_url_encrypted"]
    return result


@router.post("/")
async def create_store(store: StoreCreate):
    """Register a new store."""
    encrypted_db_url = encrypt(store.db_url)

    row = await db.fetch_one(
        """INSERT INTO stores (name, owner_name, owner_contact, app_url,
                               railway_service_id, railway_project_id, db_url_encrypted)
           VALUES (:name, :owner_name, :owner_contact, :app_url,
                   :railway_service_id, :railway_project_id, :db_url_encrypted)
           RETURNING id""",
        {
            "name": store.name,
            "owner_name": store.owner_name,
            "owner_contact": store.owner_contact,
            "app_url": store.app_url,
            "railway_service_id": store.railway_service_id,
            "railway_project_id": store.railway_project_id,
            "db_url_encrypted": encrypted_db_url,
        },
    )
    store_id = str(row["id"])
    await _audit("create_store", store_id, f"Created store: {store.name}")
    logger.info(f"Store created: {store.name} ({store_id})")
    return {"id": store_id, "name": store.name}


# Whitelist of columns allowed in store updates (prevents SQL injection via field names)
_ALLOWED_STORE_UPDATE_COLUMNS = {
    "name", "owner_name", "owner_contact", "app_url",
    "railway_service_id", "railway_project_id", "status",
}


@router.put("/{store_id}")
async def update_store(store_id: str, update: StoreUpdate):
    """Update store metadata (not credentials)."""
    existing = await db.fetch_one("SELECT id FROM stores WHERE id = :id", {"id": store_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Store not found")

    fields = {k: v for k, v in update.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Only allow whitelisted column names
    invalid = set(fields.keys()) - _ALLOWED_STORE_UPDATE_COLUMNS
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid fields: {sorted(invalid)}")

    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    fields["id"] = store_id
    await db.execute(f"UPDATE stores SET {set_clause} WHERE id = :id", fields)

    await _audit("update_store", store_id, f"Updated fields: {list(fields.keys())}")
    return {"ok": True}


@router.delete("/{store_id}")
async def delete_store(store_id: str):
    """Remove a store and all its credentials."""
    existing = await db.fetch_one("SELECT name FROM stores WHERE id = :id", {"id": store_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Store not found")

    # Audit before DELETE: master_audit_log.store_id FK must still resolve to stores.id
    await _audit("delete_store", store_id, f"Deleted store: {existing['name']}")
    await db.execute("DELETE FROM stores WHERE id = :id", {"id": store_id})
    return {"ok": True}


# ── Credentials ──────────────────────────────────────────────

@router.get("/{store_id}/credentials")
async def list_credentials(store_id: str):
    """List all credentials for a store (masked values)."""
    rows = await db.fetch_all(
        "SELECT id, key, value_encrypted, updated_at FROM store_credentials WHERE store_id = :store_id ORDER BY key",
        {"store_id": store_id},
    )
    result = []
    for row in rows:
        try:
            decrypted = decrypt(row["value_encrypted"])
            masked_value = mask(decrypted)
        except Exception:
            masked_value = "****"
        result.append({
            "id": str(row["id"]),
            "key": row["key"],
            "value_masked": masked_value,
            "updated_at": str(row["updated_at"]),
        })
    return result


@router.post("/{store_id}/credentials")
async def set_credential(store_id: str, cred: CredentialSet):
    """Set or update a credential for a store."""
    existing = await db.fetch_one("SELECT id FROM stores WHERE id = :id", {"id": store_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Store not found")

    encrypted_value = encrypt(cred.value)

    await db.execute(
        """INSERT INTO store_credentials (store_id, key, value_encrypted, updated_at)
           VALUES (:store_id, :key, :value_encrypted, NOW())
           ON CONFLICT (store_id, key) DO UPDATE SET value_encrypted = :value_encrypted, updated_at = NOW()""",
        {"store_id": store_id, "key": cred.key, "value_encrypted": encrypted_value},
    )
    await _audit("set_credential", store_id, f"Updated credential: {cred.key}")
    return {"ok": True, "key": cred.key}


@router.delete("/{store_id}/credentials/{key}")
async def delete_credential(store_id: str, key: str):
    """Delete a credential."""
    await db.execute(
        "DELETE FROM store_credentials WHERE store_id = :store_id AND key = :key",
        {"store_id": store_id, "key": key},
    )
    await _audit("delete_credential", store_id, f"Deleted credential: {key}")
    return {"ok": True}


# ── Store DB helpers ─────────────────────────────────────────

# Available models per provider (must match store app's AVAILABLE_MODELS)
AVAILABLE_MODELS = {
    "openai": ["gpt-5.4-nano", "gpt-5.4-mini"],
    "anthropic": ["claude-haiku-4-5", "claude-sonnet-4-6"],
}
VALID_ORCHESTRATION_MODES = {"legacy", "shadow", "multi_agent"}
VALID_EXCHANGE_RATE_REFERENCES = {"usd_bcv", "eur_bcv", "usdt_binance", "manual"}


def _decode_setting_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


async def _get_store_row(store_id: str):
    store = await db.fetch_one(
        "SELECT db_url_encrypted, name FROM stores WHERE id = :id", {"id": store_id}
    )
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    return store


async def _get_store_connection(store_id: str):
    store = await _get_store_row(store_id)
    try:
        store_db_url = decrypt(store["db_url_encrypted"])
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Cannot decrypt store database URL") from exc
    store_db = await _get_store_db(store_db_url)
    return store, store_db


async def _ensure_store_runtime_defaults(store_db: db_lib.Database):
    await _ensure_store_exchange_rate_migration(store_db)
    query = (
        "INSERT INTO settings (key, value) VALUES (:key, :val) "
        "ON CONFLICT (key) DO NOTHING"
    )
    for key, value in DEFAULT_RUNTIME_SETTINGS.items():
        await store_db.execute(query, {"key": key, "val": json.dumps(value)})


async def _ensure_store_exchange_rate_migration(store_db: db_lib.Database):
    legacy_row = await store_db.fetch_one(
        "SELECT value FROM settings WHERE key = 'accepted_exchange_rate'"
    )
    if not legacy_row:
        return
    legacy_value = _decode_setting_value(legacy_row["value"])
    if not str(legacy_value or "").strip():
        return

    manual_row = await store_db.fetch_one(
        "SELECT value FROM settings WHERE key = 'manual_exchange_rate'"
    )
    manual_value = _decode_setting_value(manual_row["value"]) if manual_row else ""
    if not str(manual_value or "").strip():
        await store_db.execute(
            """
            INSERT INTO settings (key, value)
            VALUES ('manual_exchange_rate', :val)
            ON CONFLICT (key) DO UPDATE SET value = :val, updated_at = NOW()
            """,
            {"val": json.dumps(legacy_value)},
        )

    reference_row = await store_db.fetch_one(
        "SELECT value FROM settings WHERE key = 'exchange_rate_reference'"
    )
    if not reference_row:
        await store_db.execute(
            """
            INSERT INTO settings (key, value)
            VALUES ('exchange_rate_reference', :val)
            ON CONFLICT (key) DO NOTHING
            """,
            {"val": json.dumps("manual")},
        )


async def _read_store_runtime_settings(store_db: db_lib.Database) -> dict:
    await _ensure_store_runtime_defaults(store_db)
    rows = await store_db.fetch_all(
        "SELECT key, value FROM settings WHERE key = ANY(:keys)",
        {"keys": list(SYNCABLE_RUNTIME_SETTING_KEYS)},
    )
    settings = {key: DEFAULT_RUNTIME_SETTINGS[key] for key in SYNCABLE_RUNTIME_SETTING_KEYS}
    for row in rows:
        settings[row["key"]] = _decode_setting_value(row["value"])
    return settings


def _normalize_runtime_fields(fields: dict, current_settings: dict) -> dict:
    invalid = set(fields) - MASTER_EDITABLE_RUNTIME_SETTING_KEYS
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid settings: {sorted(invalid)}")

    normalized = dict(fields)

    if "llm_temperature" in normalized:
        temp = float(normalized["llm_temperature"])
        if not (0.0 <= temp <= 1.0):
            raise HTTPException(status_code=400, detail="Temperature must be between 0.0 and 1.0")
        normalized["llm_temperature"] = temp

    if "llm_max_tokens" in normalized:
        tokens = int(normalized["llm_max_tokens"])
        if not (100 <= tokens <= 2000):
            raise HTTPException(status_code=400, detail="Max tokens must be between 100 and 2000")
        normalized["llm_max_tokens"] = tokens

    if "max_conversation_history" in normalized:
        history = int(normalized["max_conversation_history"])
        if not (5 <= history <= 50):
            raise HTTPException(status_code=400, detail="Conversation history must be between 5 and 50")
        normalized["max_conversation_history"] = history

    if "ai_orchestration_mode" in normalized:
        mode = str(normalized["ai_orchestration_mode"] or "").strip().lower()
        if mode not in VALID_ORCHESTRATION_MODES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid orchestration mode '{normalized['ai_orchestration_mode']}'",
            )
        normalized["ai_orchestration_mode"] = mode

    if "exchange_rate_reference" in normalized:
        reference = str(normalized["exchange_rate_reference"] or "").strip().lower()
        if reference not in VALID_EXCHANGE_RATE_REFERENCES:
            raise HTTPException(status_code=400, detail=f"Invalid exchange rate reference: {reference}")
        normalized["exchange_rate_reference"] = reference

    if "manual_exchange_rate" in normalized:
        manual_rate = str(normalized["manual_exchange_rate"] or "").strip()
        if manual_rate:
            from decimal import Decimal, InvalidOperation

            try:
                rate = Decimal(manual_rate.replace(",", "."))
            except InvalidOperation as exc:
                raise HTTPException(status_code=400, detail="Manual exchange rate must be numeric") from exc
            if rate <= 0:
                raise HTTPException(status_code=400, detail="Manual exchange rate must be greater than 0")
            normalized["manual_exchange_rate"] = format(rate.normalize(), "f")
        else:
            normalized["manual_exchange_rate"] = ""

    if "catalog_pdf_interval_hours" in normalized:
        hours = int(normalized["catalog_pdf_interval_hours"])
        if not (1 <= hours <= 168):
            raise HTTPException(status_code=400, detail="Catalog PDF interval must be between 1 and 168 hours")
        normalized["catalog_pdf_interval_hours"] = hours

    if "catalog_refresh_minutes" in normalized:
        minutes = int(normalized["catalog_refresh_minutes"])
        if not (1 <= minutes <= 1440):
            raise HTTPException(status_code=400, detail="Catalog refresh must be between 1 and 1440 minutes")
        normalized["catalog_refresh_minutes"] = minutes

    if "broadcast_check_interval_minutes" in normalized:
        minutes = int(normalized["broadcast_check_interval_minutes"])
        if not (1 <= minutes <= 60):
            raise HTTPException(status_code=400, detail="Broadcast check interval must be between 1 and 60 minutes")
        normalized["broadcast_check_interval_minutes"] = minutes

    for key in ("token_reminder_hour", "daily_analytics_hour"):
        if key in normalized:
            hour = int(normalized[key])
            if not (0 <= hour <= 23):
                raise HTTPException(status_code=400, detail=f"{key} must be between 0 and 23")
            normalized[key] = hour

    for key in ("token_reminder_minute", "daily_analytics_minute"):
        if key in normalized:
            minute = int(normalized[key])
            if not (0 <= minute <= 59):
                raise HTTPException(status_code=400, detail=f"{key} must be between 0 and 59")
            normalized[key] = minute

    provider = normalized.get("llm_provider", current_settings.get("llm_provider", "openai"))
    if provider not in AVAILABLE_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")

    if "llm_provider" in normalized and "llm_model" not in normalized:
        normalized["llm_model"] = AVAILABLE_MODELS[provider][0]

    if "llm_model" in normalized:
        model = str(normalized["llm_model"])
        if model not in AVAILABLE_MODELS.get(provider, []):
            raise HTTPException(status_code=400, detail=f"Model '{model}' not available for '{provider}'")
        normalized["llm_model"] = model

    fallback_provider = normalized.get(
        "fallback_provider", current_settings.get("fallback_provider", "anthropic")
    )
    if fallback_provider not in AVAILABLE_MODELS:
        raise HTTPException(status_code=400, detail=f"Unknown fallback provider: {fallback_provider}")

    if "fallback_provider" in normalized and "fallback_model" not in normalized:
        normalized["fallback_model"] = AVAILABLE_MODELS[fallback_provider][0]

    if "fallback_model" in normalized:
        fallback_model = str(normalized["fallback_model"])
        if fallback_model not in AVAILABLE_MODELS.get(fallback_provider, []):
            raise HTTPException(
                status_code=400,
                detail=f"Model '{fallback_model}' not available for '{fallback_provider}'",
            )
        normalized["fallback_model"] = fallback_model

    for key in ("auto_fallback", "ai_enabled"):
        if key in normalized:
            normalized[key] = bool(normalized[key])

    return normalized


async def _write_store_runtime_settings(store_db: db_lib.Database, fields: dict):
    for key, value in fields.items():
        await store_db.execute(
            """INSERT INTO settings (key, value) VALUES (:key, :val)
               ON CONFLICT (key) DO UPDATE SET value = :val, updated_at = NOW()""",
            {"key": key, "val": json.dumps(value)},
        )


async def sync_exchange_rates_to_active_stores() -> dict:
    rows = await exchange_rate_service.get_current_exchange_rates()
    settings = exchange_rate_service.build_store_rate_settings(rows)
    if not settings:
        return {"stores_synced": 0, "settings": [], "errors": []}

    stores = await db.fetch_all(
        "SELECT id, name, db_url_encrypted FROM stores WHERE status = 'active' ORDER BY created_at"
    )
    errors = []
    synced = 0

    async def _sync_store(store):
        store_id = str(store["id"])
        try:
            async with _stats_semaphore():
                store_db_url = decrypt(store["db_url_encrypted"])
                store_db = await _get_store_db(store_db_url)
                await _write_store_runtime_settings(store_db, settings)
            return {"ok": True, "store_id": store_id}
        except Exception as exc:
            logger.warning("Could not sync exchange rates to store %s: %s", store_id, exc)
            return {"ok": False, "store_id": store_id, "error": "Could not sync store"}

    results = await asyncio.gather(*[_sync_store(store) for store in stores])
    for result in results:
        if result["ok"]:
            synced += 1
        else:
            errors.append(result)
    return {"stores_synced": synced, "settings": sorted(settings), "errors": errors}


async def refresh_and_sync_exchange_rates(*, include_bcv: bool = True, include_usdt: bool = True) -> dict:
    refresh = await exchange_rate_service.refresh_exchange_rates(include_bcv=include_bcv, include_usdt=include_usdt)
    sync = await sync_exchange_rates_to_active_stores()
    return {"refresh": refresh, "sync": sync}


# ── Exchange Rates ───────────────────────────────────────────

@router.get("/exchange-rates/current")
async def get_exchange_rates_current():
    rows = await exchange_rate_service.get_current_exchange_rates()
    return exchange_rate_service.as_public_payload(rows)


@router.post("/exchange-rates/refresh")
async def force_exchange_rates_refresh():
    result = await refresh_and_sync_exchange_rates(include_bcv=True, include_usdt=True)
    await _audit("refresh_exchange_rates", None, f"Refresh status: {result['refresh'].get('status')}")
    rows = await exchange_rate_service.get_current_exchange_rates()
    return {**result, "current": exchange_rate_service.as_public_payload(rows)}


# ── Store Stats (read from store's own DB) ───────────────────

@router.get("/{store_id}/stats")
async def get_store_stats(store_id: str):
    """
    Connect to a store's own database and pull today's stats.
    Uses a single combined query + settings query for speed.
    """
    async with _stats_semaphore():
        try:
            store, store_db = await _get_store_connection(store_id)

            # Single combined query for counts
            stats_row, settings_rows = await asyncio.gather(
                store_db.fetch_one(
                    """SELECT
                        (SELECT COUNT(*) FROM conversations WHERE created_at >= CURRENT_DATE) as convos,
                        (SELECT COUNT(*) FROM orders WHERE created_at >= CURRENT_DATE) as orders,
                        (SELECT COUNT(*) FROM customers) as customers"""
                ),
                store_db.fetch_all("SELECT key, value FROM settings"),
            )
            settings = {row["key"]: _decode_setting_value(row["value"]) for row in settings_rows}

            return {
                "store_name": store["name"],
                "today_conversations": stats_row["convos"] if stats_row else 0,
                "today_orders": stats_row["orders"] if stats_row else 0,
                "total_customers": stats_row["customers"] if stats_row else 0,
                "llm_provider": settings.get("llm_provider", "unknown"),
                "llm_model": settings.get("llm_model", "unknown"),
                "ai_enabled": settings.get("ai_enabled", True),
                "ai_orchestration_mode": settings.get("ai_orchestration_mode", "legacy"),
            }
        except Exception as e:
            logger.warning(f"Could not fetch stats for store {store_id}: {e}")
            return {
                "store_name": store["name"] if 'store' in locals() else store_id,
                "error": "Could not connect to store database",
            }


# ── Runtime Settings (read/write on store's DB) ─────────────


@router.get("/{store_id}/settings")
async def get_store_settings(store_id: str):
    """Read dashboard-managed runtime settings from a store database."""
    async with _stats_semaphore():
        try:
            store, store_db = await _get_store_connection(store_id)
            settings = await _read_store_runtime_settings(store_db)
            return {
                "store_name": store["name"],
                "settings": settings,
                "available_models": AVAILABLE_MODELS,
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"Could not read runtime settings for store {store_id}: {e}")
            raise HTTPException(status_code=502, detail="Could not connect to store database") from e


@router.put("/{store_id}/settings")
async def update_store_settings(store_id: str, update: RuntimeSettingsUpdate, request: Request):
    """Write dashboard-managed runtime settings to a store database."""
    raw_fields = await request.json()
    if isinstance(raw_fields, dict):
        blocked = set(raw_fields) & BLOCKED_PAYMENT_SETTING_KEYS
        if blocked:
            raise HTTPException(
                status_code=403,
                detail="Payment methods are managed only from the store dashboard.",
            )
        rate_fields = set(raw_fields) & PROVIDER_EXCHANGE_RATE_SETTING_KEYS
        if rate_fields:
            raise HTTPException(
                status_code=403,
                detail="Provider exchange rates are synchronized by the master refresh job.",
            )

    fields = {k: v for k, v in update.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    async with _stats_semaphore():
        try:
            store, store_db = await _get_store_connection(store_id)
            current_settings = await _read_store_runtime_settings(store_db)
            normalized = _normalize_runtime_fields(fields, current_settings)
            await _write_store_runtime_settings(store_db, normalized)
            await _audit("update_runtime_settings", store_id, f"Updated: {sorted(normalized.keys())}")
            return {"ok": True, "store": store["name"], "updated": sorted(normalized.keys())}
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"Could not update runtime settings for store {store_id}: {e}")
            raise HTTPException(status_code=502, detail="Could not write to store database") from e


@router.get("/{store_id}/llm-settings")
async def get_llm_settings(store_id: str):
    """Backward-compatible LLM settings view backed by shared runtime settings."""
    data = await get_store_settings(store_id)
    settings = {
        key: value
        for key, value in data["settings"].items()
        if key.startswith("llm_")
        or key.startswith("fallback_")
        or key in {"auto_fallback", "max_conversation_history", "ai_enabled"}
    }
    return {
        "store_name": data["store_name"],
        "settings": settings,
        "available_models": data["available_models"],
    }


@router.put("/{store_id}/llm-settings")
async def update_llm_settings(store_id: str, update: LLMSettingsUpdate, request: Request):
    """Backward-compatible LLM settings write backed by shared runtime settings."""
    return await update_store_settings(store_id, update, request)


@router.get("/{store_id}/llm-usage")
async def get_llm_usage(store_id: str, days: int = 1):
    """Read LLM token usage and estimated costs from a store's database.
    Use ?days=7 for last 7 days, ?days=30 for last month, etc."""
    days = max(1, min(days, 90))
    cutoff = date.today() - timedelta(days=days - 1)

    store = await db.fetch_one(
        "SELECT db_url_encrypted, name FROM stores WHERE id = :id", {"id": store_id}
    )
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")

    try:
        store_db_url = decrypt(store["db_url_encrypted"])
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Cannot decrypt store database URL") from exc

    async with _stats_semaphore():
        try:
            store_db = await _get_store_db(store_db_url)
            rows = await store_db.fetch_all(
                """SELECT provider, model,
                          COUNT(*) as calls,
                          COALESCE(SUM(input_tokens), 0) as total_input,
                          COALESCE(SUM(output_tokens), 0) as total_output
                   FROM usage_log
                   WHERE created_at >= :cutoff
                   GROUP BY provider, model
                   ORDER BY provider, model""",
                {"cutoff": cutoff},
            )

            breakdown = []
            total_cost = 0.0
            for row in rows:
                cost = _estimate_cost(row["model"], row["total_input"], row["total_output"])
                total_cost += cost
                breakdown.append({
                    "provider": row["provider"],
                    "model": row["model"],
                    "calls": row["calls"],
                    "input_tokens": row["total_input"],
                    "output_tokens": row["total_output"],
                    "estimated_cost_usd": round(cost, 4),
                })

            return {
                "store_name": store["name"],
                "period": {"days": days, "from": str(cutoff)},
                "breakdown": breakdown,
                "total_estimated_cost_usd": round(total_cost, 4),
            }
        except Exception as e:
            logger.warning(f"Could not fetch LLM usage for store {store_id}: {e}")
            return {"store_name": store["name"], "error": "Could not fetch usage data"}


# Model cost rates (per 1M tokens) — must match store app's AVAILABLE_MODELS
_MODEL_COSTS = {
    "gpt-5.4-nano": {"input": 0.20, "output": 1.25},
    "gpt-5.4-mini": {"input": 0.75, "output": 4.50},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
}


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = _MODEL_COSTS.get(model, {"input": 1.0, "output": 3.0})
    return (input_tokens / 1_000_000) * rates["input"] + (output_tokens / 1_000_000) * rates["output"]


@router.get("/{store_id}/conversations")
async def get_store_conversations(
    store_id: str,
    customer_id: str = "",
    channel: str = "",
    date_from: str = "",
    date_to: str = "",
    search: str = "",
    limit: int = 50,
    offset: int = 0,
):
    """Fetch conversation messages from a store's database with filters."""
    store = await db.fetch_one(
        "SELECT db_url_encrypted, name FROM stores WHERE id = :id", {"id": store_id}
    )
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")

    try:
        store_db_url = decrypt(store["db_url_encrypted"])
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Cannot decrypt store database URL") from exc

    limit = max(1, min(limit, 200))

    async with _stats_semaphore():
        try:
            store_db = await _get_store_db(store_db_url)

            # Fetch customers for the filter dropdown
            customers_rows = await store_db.fetch_all(
                """SELECT id, display_name, phone, instagram_handle, channel
                   FROM customers ORDER BY last_active DESC LIMIT 200"""
            )
            customers_list = [
                {
                    "id": str(r["id"]),
                    "display_name": r["display_name"] or r["phone"] or r["instagram_handle"] or "Unknown",
                    "channel": r["channel"],
                }
                for r in customers_rows
            ]

            # Build conversation query with filters
            clauses = []
            params: dict = {"limit": limit, "offset": offset}
            if customer_id:
                clauses.append("c.customer_id = CAST(:customer_id AS uuid)")
                params["customer_id"] = customer_id
            if channel:
                clauses.append("c.channel = :channel")
                params["channel"] = channel
            if date_from:
                clauses.append("c.created_at >= CAST(:date_from AS date)")
                params["date_from"] = date_from
            if date_to:
                clauses.append("c.created_at < (CAST(:date_to AS date) + INTERVAL '1 day')")
                params["date_to"] = date_to
            if search:
                clauses.append("c.content ILIKE :search")
                params["search"] = f"%{search}%"

            where = (" AND " + " AND ".join(clauses)) if clauses else ""

            rows = await store_db.fetch_all(
                f"""SELECT c.id, c.customer_id, c.role, c.content, c.channel,
                           c.media_url, c.function_calls, c.created_at,
                           cu.display_name, cu.phone, cu.instagram_handle,
                           ul.provider  AS ul_provider,
                           ul.model     AS ul_model,
                           ul.input_tokens  AS ul_input,
                           ul.output_tokens AS ul_output,
                           ul.response_time_ms AS ul_ms,
                           ul.was_fallback AS ul_fallback
                    FROM conversations c
                    LEFT JOIN customers cu ON c.customer_id = cu.id
                    LEFT JOIN LATERAL (
                        SELECT provider, model, input_tokens, output_tokens,
                               response_time_ms, was_fallback
                        FROM usage_log u
                        WHERE u.customer_id = c.customer_id
                          AND u.created_at BETWEEN c.created_at - INTERVAL '10 seconds'
                                               AND c.created_at + INTERVAL '10 seconds'
                        ORDER BY ABS(EXTRACT(EPOCH FROM (u.created_at - c.created_at)))
                        LIMIT 1
                    ) ul ON c.role = 'assistant'
                    WHERE 1=1 {where}
                    ORDER BY c.created_at DESC
                    LIMIT :limit OFFSET :offset""",
                params,
            )

            # Count total for pagination
            count_row = await store_db.fetch_one(
                f"""SELECT COUNT(*) as cnt FROM conversations c WHERE 1=1 {where}""",
                {k: v for k, v in params.items() if k not in ("limit", "offset")},
            )

            messages = []
            for r in rows:
                msg = {
                    "id": str(r["id"]),
                    "customer_id": str(r["customer_id"]) if r["customer_id"] else None,
                    "customer_name": r["display_name"] or r["phone"] or r["instagram_handle"] or "Unknown",
                    "role": r["role"],
                    "content": r["content"],
                    "channel": r["channel"],
                    "media_url": r["media_url"],
                    "function_calls": r["function_calls"],
                    "created_at": str(r["created_at"]),
                }
                if r["ul_provider"]:
                    cost = _estimate_cost(r["ul_model"] or "", r["ul_input"] or 0, r["ul_output"] or 0)
                    msg["usage"] = {
                        "provider": r["ul_provider"],
                        "model": r["ul_model"],
                        "input_tokens": r["ul_input"] or 0,
                        "output_tokens": r["ul_output"] or 0,
                        "response_time_ms": r["ul_ms"],
                        "was_fallback": r["ul_fallback"] or False,
                        "estimated_cost_usd": round(cost, 6),
                    }
                messages.append(msg)

            return {
                "store_name": store["name"],
                "messages": messages,
                "total": count_row["cnt"] if count_row else 0,
                "limit": limit,
                "offset": offset,
                "customers": customers_list,
            }
        except Exception as e:
            logger.warning(f"Could not fetch conversations for store {store_id}: {e}")
            raise HTTPException(status_code=502, detail="Could not connect to store database") from e


@router.get("/llm-costs/aggregate")
async def aggregate_llm_costs(days: int = 1):
    """
    Pull LLM costs from ALL stores and return a summary.
    Use ?days=7 for last 7 days, ?days=30 for last month.
    """

    days = max(1, min(days, 90))
    cutoff = date.today() - timedelta(days=days - 1)

    stores = await db.fetch_all(
        "SELECT id, name, db_url_encrypted FROM stores WHERE status != 'deleted' ORDER BY name"
    )

    async def _fetch_store_costs(store):
        try:
            store_db_url = decrypt(store["db_url_encrypted"])
        except Exception:
            return {"store_id": str(store["id"]), "store_name": store["name"], "error": "decrypt_failed"}

        async with _stats_semaphore():
            try:
                store_db = await _get_store_db(store_db_url)
                rows = await store_db.fetch_all(
                    """SELECT provider, model,
                              COUNT(*) as calls,
                              COALESCE(SUM(input_tokens), 0) as total_input,
                              COALESCE(SUM(output_tokens), 0) as total_output
                       FROM usage_log
                       WHERE created_at >= :cutoff
                       GROUP BY provider, model""",
                    {"cutoff": cutoff},
                )

                store_cost = 0.0
                store_calls = 0
                store_breakdown = []
                for row in rows:
                    cost = _estimate_cost(row["model"], row["total_input"], row["total_output"])
                    store_cost += cost
                    store_calls += row["calls"]
                    store_breakdown.append({
                        "provider": row["provider"],
                        "model": row["model"],
                        "calls": row["calls"],
                        "input_tokens": row["total_input"],
                        "output_tokens": row["total_output"],
                        "cost_usd": round(cost, 4),
                    })

                return {
                    "store_id": str(store["id"]),
                    "store_name": store["name"],
                    "calls": store_calls,
                    "estimated_cost_usd": round(store_cost, 4),
                    "breakdown": store_breakdown,
                }
            except Exception as e:
                logger.warning(f"Could not fetch costs for store {store['id']}: {e}")
                return {"store_id": str(store["id"]), "store_name": store["name"], "error": "Could not fetch cost data"}

    results = await asyncio.gather(*[_fetch_store_costs(s) for s in stores])

    platform_total = sum(r.get("estimated_cost_usd", 0) for r in results)
    platform_calls = sum(r.get("calls", 0) for r in results)

    return {
        "period": {"days": days, "from": str(cutoff)},
        "stores": list(results),
        "platform_total_calls": platform_calls,
        "platform_total_cost_usd": round(platform_total, 4),
    }


# ── Railway Deploy ────────────────────────────────────────────

@router.get("/{store_id}/railway/status")
async def get_railway_status(store_id: str):
    """
    Check Railway deployment status for a store.
    Returns service info + latest deployment status.
    """
    config = get_config()

    store = await db.fetch_one(
        "SELECT railway_service_id, railway_project_id, name FROM stores WHERE id = :id",
        {"id": store_id},
    )
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")

    if not config.railway_api_token:
        return {"status": "not_configured", "message": "RAILWAY_API_TOKEN not set"}

    if not store["railway_service_id"]:
        return {"status": "not_linked", "message": "No Railway service ID configured for this store"}

    try:
        from app.stores.railway import get_environments, get_latest_deployment, get_service_info

        service = await get_service_info(store["railway_service_id"])
        environments = []
        deployment = None
        deployment_warning = None

        if store["railway_project_id"]:
            environments = await get_environments(store["railway_project_id"])
        else:
            deployment_warning = "No Railway project ID configured, so deployment status could not be resolved."

        environment_id = ""
        if environments:
            prod_env = next((env for env in environments if (env.get("name") or "").lower() == "production"), None)
            environment_id = (prod_env or environments[0]).get("id", "")

        if environment_id:
            try:
                deployment = await get_latest_deployment(store["railway_service_id"], environment_id)
            except Exception as e:
                deployment_warning = str(e)

        return {
            "status": "linked",
            "service": service,
            "latest_deployment": deployment,
            "environments": environments,
            "deployment_warning": deployment_warning,
        }
    except Exception as e:
        logger.warning(f"Railway status check failed for store {store_id}: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/{store_id}/deploy")
async def deploy_credentials(store_id: str, environment_id: str = ""):
    """
    Push all stored credentials to Railway as env vars, then trigger a redeploy.

    Flow:
    1. Decrypt all credentials for this store
    2. Push them to Railway as environment variables
    3. Trigger a redeploy so the changes take effect
    """
    config = get_config()

    if not config.railway_api_token:
        raise HTTPException(status_code=400, detail="RAILWAY_API_TOKEN not configured")

    store = await db.fetch_one(
        "SELECT railway_service_id, railway_project_id, name FROM stores WHERE id = :id",
        {"id": store_id},
    )
    if not store:
        raise HTTPException(status_code=404, detail="Store not found")
    if not store["railway_service_id"]:
        raise HTTPException(status_code=400, detail="No Railway service ID configured for this store")
    if not store["railway_project_id"]:
        raise HTTPException(status_code=400, detail="No Railway project ID configured for this store")

    # Get environment ID — use provided one, or find production env
    if not environment_id and store["railway_project_id"]:
        from app.stores.railway import get_environments
        envs = await get_environments(store["railway_project_id"])
        prod_env = next((e for e in envs if e["name"].lower() == "production"), None)
        if prod_env:
            environment_id = prod_env["id"]
        elif envs:
            environment_id = envs[0]["id"]

    if not environment_id:
        raise HTTPException(status_code=400, detail="Could not determine Railway environment. Provide environment_id or set railway_project_id on the store.")

    # Decrypt all credentials
    cred_rows = await db.fetch_all(
        "SELECT key, value_encrypted FROM store_credentials WHERE store_id = :store_id",
        {"store_id": store_id},
    )

    if not cred_rows:
        raise HTTPException(status_code=400, detail="No credentials to deploy")

    variables = {}
    for row in cred_rows:
        try:
            variables[row["key"]] = decrypt(row["value_encrypted"])
        except Exception as e:
            logger.error(f"Failed to decrypt credential {row['key']} for store {store_id}: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to decrypt credential: {row['key']}") from e

    # Push to Railway
    from app.stores.railway import get_latest_deployment, redeploy_service, upsert_variables

    try:
        await upsert_variables(
            store["railway_project_id"],
            store["railway_service_id"],
            environment_id,
            variables,
            skip_deploys=True,
        )
        latest_deployment = await get_latest_deployment(store["railway_service_id"], environment_id)
        if not latest_deployment or not latest_deployment.get("id"):
            raise RuntimeError("Could not determine the latest Railway deployment to redeploy.")
        deployment_id = await redeploy_service(latest_deployment["id"])
    except Exception as e:
        logger.error(f"Railway deploy failed for store {store_id}: {e}")
        await _audit("deploy_failed", store_id, f"Railway deploy failed: {e}")
        raise HTTPException(status_code=502, detail=str(e)) from e

    await _audit(
        "deploy_credentials",
        store_id,
        f"Deployed {len(variables)} credentials and triggered redeploy ({deployment_id})",
    )

    return {
        "ok": True,
        "store": store["name"],
        "credentials_pushed": len(variables),
        "deployment_id": deployment_id,
        "environment_id": environment_id,
    }


# ── Audit Log ────────────────────────────────────────────────

@router.get("/audit/log")
async def get_audit_log(
    limit: int = 50, action: str = "", store_id: str = "",
    search: str = "", date_from: str = "", date_to: str = "",
):
    """Get recent audit log entries with optional filters."""
    clauses = []
    params: dict = {"limit": limit}
    if action:
        clauses.append("al.action = :action")
        params["action"] = action
    if store_id:
        clauses.append("al.store_id = :store_id")
        params["store_id"] = store_id
    if search:
        clauses.append("al.detail ILIKE :search")
        params["search"] = f"%{search}%"
    if date_from:
        clauses.append("al.created_at >= CAST(:date_from AS date)")
        params["date_from"] = date_from
    if date_to:
        clauses.append("al.created_at < (CAST(:date_to AS date) + INTERVAL '1 day')")
        params["date_to"] = date_to
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = await db.fetch_all(
        f"""SELECT al.id, al.action, al.store_id, s.name as store_name, al.detail, al.created_at
           FROM master_audit_log al
           LEFT JOIN stores s ON al.store_id = s.id
           {where}
           ORDER BY al.created_at DESC LIMIT :limit""",
        params,
    )
    return [dict(row._mapping) for row in rows]
