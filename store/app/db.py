"""
Async database connection pool using the databases library.
Provides a thin wrapper for common queries and lightweight DB timing telemetry.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar

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

logger = logging.getLogger(__name__)
EXPECTED_SCHEMA_VERSION = 15
SLOW_DB_OPERATION_MS = 500.0

_db: _InstrumentedDatabase | None = None
_db_timings: ContextVar[dict[str, float] | None] = ContextVar("db_timings", default=None)


class _TimedTransaction:
    """Delegate a databases transaction while recording acquire/finish latency."""

    def __init__(self, transaction):
        self._transaction = transaction

    async def __aenter__(self):
        started = time.perf_counter()
        try:
            return await self._transaction.__aenter__()
        finally:
            _record_db_timing("transaction_acquire_ms", _elapsed_ms(started))

    async def __aexit__(self, exc_type, exc, tb):
        started = time.perf_counter()
        try:
            return await self._transaction.__aexit__(exc_type, exc, tb)
        finally:
            stage = "transaction_rollback_ms" if exc_type is not None else "transaction_commit_ms"
            _record_db_timing(stage, _elapsed_ms(started))

    def __getattr__(self, name):
        return getattr(self._transaction, name)


class _InstrumentedDatabase:
    """Transparent databases.Database wrapper with bounded timing instrumentation."""

    def __init__(self, database):
        self._database = database

    async def connect(self):
        return await self._database.connect()

    async def disconnect(self):
        return await self._database.disconnect()

    def transaction(self, *args, **kwargs):
        return _TimedTransaction(self._database.transaction(*args, **kwargs))

    async def fetch_one(self, *args, **kwargs):
        return await self._timed_call("fetch_one", self._database.fetch_one, *args, **kwargs)

    async def fetch_all(self, *args, **kwargs):
        return await self._timed_call("fetch_all", self._database.fetch_all, *args, **kwargs)

    async def execute(self, *args, **kwargs):
        return await self._timed_call("execute", self._database.execute, *args, **kwargs)

    async def execute_many(self, *args, **kwargs):
        return await self._timed_call("execute_many", self._database.execute_many, *args, **kwargs)

    async def _timed_call(self, operation: str, call, *args, **kwargs):
        query = kwargs.get("query")
        if query is None and args:
            query = args[0]
        stage = _query_timing_stage(operation, str(query or ""))
        started = time.perf_counter()
        try:
            return await call(*args, **kwargs)
        finally:
            elapsed_ms = _elapsed_ms(started)
            _record_db_timing(stage, elapsed_ms)
            if elapsed_ms >= SLOW_DB_OPERATION_MS:
                logger.warning(
                    "Slow database operation: stage=%s elapsed_ms=%.1f",
                    stage,
                    elapsed_ms,
                )

    def __getattr__(self, name):
        return getattr(self._database, name)


@contextmanager
def collect_query_timings():
    """Collect DB timing totals for the current async context only."""
    timings: dict[str, float] = {}
    token = _db_timings.set(timings)
    try:
        yield timings
    finally:
        _db_timings.reset(token)


def _record_db_timing(stage: str, elapsed_ms: float) -> None:
    timings = _db_timings.get()
    if timings is not None:
        timings[stage] = timings.get(stage, 0.0) + elapsed_ms


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _query_timing_stage(operation: str, query: str) -> str:
    compact = " ".join(query.upper().split())
    if "PG_ADVISORY_XACT_LOCK" in compact:
        return "advisory_lock_ms"
    if "FROM KOMMO_MESSAGE_RECEIPTS" in compact and "EXTERNAL_MESSAGE_ID" in compact:
        return "duplicate_lookup_ms"
    if (
        "FROM KOMMO_MESSAGE_JOBS" in compact
        and "STATUS = 'PENDING'" in compact
        and "FOR UPDATE" in compact
    ):
        return "pending_lookup_ms"
    if "INSERT INTO KOMMO_MESSAGE_RECEIPTS" in compact:
        return "receipt_insert_ms"
    if "INSERT INTO KOMMO_MESSAGE_JOBS" in compact or "UPDATE KOMMO_MESSAGE_JOBS" in compact:
        return "job_mutation_ms"
    return f"db_{operation}_ms"


async def connect():
    """Initialize the bounded connection pool. Called once at app startup."""
    global _db
    config = get_config()
    min_size = int(config.database_pool_min_size)
    max_size = int(config.database_pool_max_size)
    if min_size > max_size:
        raise RuntimeError(
            "DATABASE_POOL_MIN_SIZE cannot be greater than DATABASE_POOL_MAX_SIZE"
        )
    raw_db = databases.Database(
        config.database_url,
        min_size=min_size,
        max_size=max_size,
    )
    _db = _InstrumentedDatabase(raw_db)
    await _db.connect()
    logger.info("Database pool connected: min_size=%s max_size=%s", min_size, max_size)


async def disconnect():
    """Close the pool. Called at app shutdown."""
    if _db:
        await _db.disconnect()


def get_db() -> _InstrumentedDatabase:
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
    accepted_versions = (
        {1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15},
        {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15},
    )
    if versions not in accepted_versions:
        raise RuntimeError(
            f"Store database schema version mismatch: expected one of {[sorted(item) for item in accepted_versions]}, found {sorted(versions)}. "
            "Apply Store migrations through store/migrations/015_conversation_interaction_scope.sql."
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


def validate_runtime_provider_settings(
    settings: dict,
    available_providers: list[str] | set[str] | tuple[str, ...] | None = None,
) -> None:
    """Fail fast when stored fallback settings reference an unavailable provider.

    During isolated unit tests provider initialization may intentionally not have run.
    In that case an empty provider registry is ignored. Application startup calls
    ``get_settings`` after ``init_providers``, so production configuration is validated.
    """
    if not settings.get("auto_fallback", True):
        return

    if available_providers is None:
        from app.ai.providers import list_providers

        available_providers = list_providers()

    available = set(available_providers)
    if not available:
        return

    fallback_provider = str(settings.get("fallback_provider") or "anthropic")
    if fallback_provider not in available:
        raise RuntimeError(
            "Runtime LLM fallback configuration is invalid: "
            f"auto_fallback is enabled but fallback provider '{fallback_provider}' is not initialized. "
            f"Available providers: {sorted(available)}. "
            "Configure the provider API key, select an available fallback provider, or disable auto_fallback."
        )


async def get_settings() -> dict:
    """
    Return all settings as a dict.
    Cache is reused only when the latest settings row has not changed.
    Example: {"llm_provider": "openai", "llm_model": "gpt-5.6-luna", ...}
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
        validate_runtime_provider_settings(_settings_cache)
        return _settings_cache

    rows = await fetch_all("SELECT key, value FROM settings")
    _settings_cache = {row["key"]: _decode_setting_value(row["value"]) for row in rows}
    _settings_ts = now
    _settings_version = current_version
    validate_runtime_provider_settings(_settings_cache)
    return _settings_cache


def invalidate_settings_cache():
    """Force the next get_settings() call to hit the DB."""
    global _settings_cache, _settings_version
    _settings_cache = None
    _settings_version = None
