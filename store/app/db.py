"""
Async database connection pool using the databases library.
Provides a thin wrapper for common queries.
"""

import json
import time
import databases
from app.config import get_config

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


async def get_settings() -> dict:
    """
    Return all settings as a dict, cached for 60 seconds.
    Example: {"llm_provider": "openai", "llm_model": "gpt-5.4-nano", ...}
    """
    global _settings_cache, _settings_ts
    now = time.time()

    if _settings_cache is not None and (now - _settings_ts) < 60:
        return _settings_cache

    rows = await fetch_all("SELECT key, value FROM settings")
    _settings_cache = {row["key"]: json.loads(row["value"]) if isinstance(row["value"], str) else row["value"] for row in rows}
    _settings_ts = now
    return _settings_cache


def invalidate_settings_cache():
    """Force the next get_settings() call to hit the DB."""
    global _settings_cache
    _settings_cache = None
