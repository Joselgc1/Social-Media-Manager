"""
Async database connection pool using the databases library.
Provides a thin wrapper for common queries.
"""

import json
import time
import databases
from app.config import get_config
from app.runtime_settings import RUNTIME_SETTING_DEFAULTS
from app.payment_methods import (
    LEGACY_PAYMENT_SETTING_KEYS,
    PAYMENT_METHODS_SETTING_KEY,
    build_payment_methods_from_legacy,
)

_db: databases.Database | None = None


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
    return await get_db().fetch_one(query=query, values=values or {})


async def fetch_all(query: str, values: dict | None = None):
    return await get_db().fetch_all(query=query, values=values or {})


async def execute(query: str, values: dict | None = None):
    return await get_db().execute(query=query, values=values or {})


# ── Settings cache ───────────────────────────────────────────

_settings_cache: dict | None = None
_settings_ts: float = 0
_settings_version: str | None = None


async def ensure_default_settings():
    """Insert any missing runtime settings without overwriting existing values."""
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
