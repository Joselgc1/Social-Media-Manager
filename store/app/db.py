"""
Async database connection pool using the databases library.
Provides a thin wrapper for common queries.
"""

import json
import logging
import time

import databases

from app.config import get_config
from app.exchange_rates import (
    LEGACY_ACCEPTED_EXCHANGE_RATE_KEY,
    MANUAL_EXCHANGE_RATE_KEY,
)
from app.payment_methods import (
    LEGACY_PAYMENT_SETTING_KEYS,
    PAYMENT_METHODS_SETTING_KEY,
    build_payment_methods_from_legacy,
)
from app.runtime_settings import RUNTIME_SETTING_DEFAULTS

_db: databases.Database | None = None
logger = logging.getLogger(__name__)
EXPECTED_SCHEMA_VERSION = 6


async def connect():
    """Initialize the connection pool. Called once at app startup."""
    global _db
    config = get_config()
    _db = databases.Database(config.database_url)
    await _db.connect()


async def disconnect():
    """Close the pool. Called at app shutdown."""
    if _db:
        await _db.disconnect()


def get_db() -> databases.Database:
    """Return the active database instance."""
    if _db is None:
        raise RuntimeError("Database not connected. Call connect() first.")
    return _db


# ── Convenience helpers ──────────────────────────────────────

async def fetch_one(query: str, values: dict | None = None):
    try:
        return await get_db().fetch_one(query=query, values=values or {})
    except Exception as e:
        _log_query_error("fetch_one", query, values, e)
        raise


async def fetch_all(query: str, values: dict | None = None):
    try:
        return await get_db().fetch_all(query=query, values=values or {})
    except Exception as e:
        _log_query_error("fetch_all", query, values, e)
        raise


async def execute(query: str, values: dict | None = None):
    try:
        return await get_db().execute(query=query, values=values or {})
    except Exception as e:
        _log_query_error("execute", query, values, e)
        raise


async def verify_schema_version() -> None:
    """Fail startup before application queries run against an old schema."""
    try:
        rows = await fetch_all("SELECT version FROM schema_migrations ORDER BY version")
    except Exception as e:
        raise RuntimeError(
                "Store database schema is unversioned. Apply store/migrations/001_schema.sql to a fresh database."
        ) from e

    versions = {int(row["version"]) for row in rows}
    accepted_versions = ({1, 3, 4, 5, 6}, {1, 2, 3, 4, 5, 6})
    if versions not in accepted_versions:
        raise RuntimeError(
            f"Store database schema version mismatch: expected one of {[sorted(item) for item in accepted_versions]}, found {sorted(versions)}. "
            "Apply Store migrations through store/migrations/006_instagram_story_context.sql."
        )

    required_meta_columns = {
        "processing_heartbeat_at",
        "processing_lease_token",
        "outbound_started_at",
        "outbound_message_ids",
    }
    column_rows = await fetch_all(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'meta_inbound_jobs'
        """
    )
    actual_meta_columns = {str(row["column_name"]) for row in column_rows}
    missing_columns = required_meta_columns - actual_meta_columns
    if missing_columns:
        raise RuntimeError(
            "Store database schema is missing Meta inbound lease-fencing columns: "
            f"{', '.join(sorted(missing_columns))}. "
            "Apply store/migrations/002_consolidated_upgrade.sql."
        )


def _log_query_error(operation: str, query: str, values: dict | None, error: Exception) -> None:
    message = str(error)
    if "bound parameter" not in message:
        return
    compact_query = " ".join(str(query).split())[:500]
    logger.error(
        "Database %s failed while binding query params: value_keys=%s query=%s error=%s",
        operation,
        sorted((values or {}).keys()),
        compact_query,
        message,
    )


# ── Settings cache ───────────────────────────────────────────

_settings_cache: dict | None = None
_settings_ts: float = 0
_settings_version: str | None = None


async def ensure_default_settings():
    """Insert any missing runtime settings without overwriting existing values."""
    await _ensure_exchange_rate_settings()
    rows = [
        {"key": key, "val": json.dumps(value)}
        for key, value in RUNTIME_SETTING_DEFAULTS.items()
        if key != PAYMENT_METHODS_SETTING_KEY
    ]
    query = (
        "INSERT INTO settings (key, value) VALUES (:key, :val) "
        "ON CONFLICT (key) DO NOTHING"
    )
    async with get_db().transaction():
        for row in rows:
            await get_db().execute(query=query, values=row)

    await _ensure_payment_methods_setting()
    invalidate_settings_cache()


async def _ensure_exchange_rate_settings():
    legacy_row = await fetch_one(
        "SELECT value FROM settings WHERE key = :key",
        {"key": LEGACY_ACCEPTED_EXCHANGE_RATE_KEY},
    )
    if not legacy_row:
        return

    legacy_value = _decode_setting_value(legacy_row["value"])
    if not str(legacy_value or "").strip():
        return

    manual_row = await fetch_one(
        "SELECT value FROM settings WHERE key = :key",
        {"key": MANUAL_EXCHANGE_RATE_KEY},
    )
    manual_value = _decode_setting_value(manual_row["value"]) if manual_row else ""
    if not str(manual_value or "").strip():
        await execute(
            """
            INSERT INTO settings (key, value)
            VALUES (:key, :val)
            ON CONFLICT (key) DO UPDATE SET value = :val, updated_at = NOW()
            """,
            {"key": MANUAL_EXCHANGE_RATE_KEY, "val": json.dumps(legacy_value)},
        )

    reference_row = await fetch_one(
        "SELECT value FROM settings WHERE key = 'exchange_rate_reference'"
    )
    if not reference_row:
        await execute(
            """
            INSERT INTO settings (key, value)
            VALUES ('exchange_rate_reference', :val)
            ON CONFLICT (key) DO NOTHING
            """,
            {"val": json.dumps("manual")},
        )


async def _ensure_payment_methods_setting():
    row = await fetch_one(
        "SELECT value FROM settings WHERE key = :key",
        {"key": PAYMENT_METHODS_SETTING_KEY},
    )
    if row:
        return

    legacy_rows = await fetch_all(
        "SELECT key, value FROM settings WHERE key = ANY(:keys)",
        {"keys": list(LEGACY_PAYMENT_SETTING_KEYS)},
    )
    legacy_settings = {
        legacy_row["key"]: _decode_setting_value(legacy_row["value"])
        for legacy_row in legacy_rows
    }
    payment_methods = build_payment_methods_from_legacy(legacy_settings)

    await execute(
        """
        INSERT INTO settings (key, value)
        VALUES (:key, :val)
        ON CONFLICT (key) DO NOTHING
        """,
        {
            "key": PAYMENT_METHODS_SETTING_KEY,
            "val": json.dumps(payment_methods, ensure_ascii=False),
        },
    )


def _decode_setting_value(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


async def get_settings() -> dict:
    """
    Return all settings as a dict.
    Cache is reused only when the latest settings row has not changed.
    Example: {"llm_provider": "openai", "llm_model": "gpt-5.4-nano", ...}
    """
    global _settings_cache, _settings_ts, _settings_version
    now = time.time()
    version_row = await fetch_one("SELECT MAX(updated_at)::text as version FROM settings")
    current_version = version_row["version"] if version_row else None

    if (
        _settings_cache is not None
        and _settings_version == current_version
        and (now - _settings_ts) < 60
    ):
        return _settings_cache

    rows = await fetch_all("SELECT key, value FROM settings")
    _settings_cache = {row["key"]: _decode_setting_value(row["value"]) for row in rows}
    _settings_ts = now
    _settings_version = current_version
    return _settings_cache


def invalidate_settings_cache():
    """Force the next get_settings() call to hit the DB."""
    global _settings_cache, _settings_version
    _settings_cache = None
    _settings_version = None
