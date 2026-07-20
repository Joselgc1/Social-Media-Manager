"""Master-side exchange-rate persistence, refresh, freshness, and store sync payloads."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app import db
from app.config import get_config
from app.stores.dolarvzla import DolarVzlaClient, NormalizedExchangeRate

logger = logging.getLogger(__name__)

RATE_KEYS = ("usd_bcv", "eur_bcv", "usdt_binance")
_refresh_lock = asyncio.Lock()


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _datetime_or_none(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _rate_changed(existing: Any, rate: NormalizedExchangeRate) -> bool:
    if not existing:
        return True
    return any(
        (
            str(existing["currency_code"]) != rate.currency_code,
            str(existing["market"]) != rate.market,
            _decimal_or_none(existing["rate"]) != rate.rate,
            _datetime_or_none(existing["effective_at"]) != rate.effective_at,
            _decimal_or_none(existing["previous_rate"]) != rate.previous_rate,
            _decimal_or_none(existing["change_percentage"]) != rate.change_percentage,
            str(existing["source"]) != rate.source,
        )
    )


async def upsert_exchange_rate(rate: NormalizedExchangeRate) -> bool:
    existing = await db.fetch_one(
        "SELECT currency_code, market, rate, effective_at, previous_rate, change_percentage, source "
        "FROM exchange_rates WHERE rate_key = :rate_key",
        {"rate_key": rate.rate_key},
    )
    changed = _rate_changed(existing, rate)
    await db.execute(
        """
        INSERT INTO exchange_rates (
            rate_key, currency_code, market, rate, effective_at, fetched_at,
            previous_rate, change_percentage, source, updated_at
        ) VALUES (
            :rate_key, :currency_code, :market, :rate, :effective_at, :fetched_at,
            :previous_rate, :change_percentage, :source, NOW()
        )
        ON CONFLICT (rate_key) DO UPDATE SET
            currency_code = EXCLUDED.currency_code,
            market = EXCLUDED.market,
            rate = EXCLUDED.rate,
            effective_at = EXCLUDED.effective_at,
            fetched_at = EXCLUDED.fetched_at,
            previous_rate = EXCLUDED.previous_rate,
            change_percentage = EXCLUDED.change_percentage,
            source = EXCLUDED.source,
            updated_at = NOW()
        """,
        {
            "rate_key": rate.rate_key,
            "currency_code": rate.currency_code,
            "market": rate.market,
            "rate": rate.rate,
            "effective_at": rate.effective_at,
            "fetched_at": rate.fetched_at,
            "previous_rate": rate.previous_rate,
            "change_percentage": rate.change_percentage,
            "source": rate.source,
        },
    )
    logger.info(
        "DolarVZLA rate refresh success",
        extra={
            "source": rate.source,
            "rate_key": rate.rate_key,
            "success": True,
            "effective_at": rate.effective_at.isoformat(),
            "changed": changed,
        },
    )
    return changed


async def get_current_exchange_rates() -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        """
        SELECT rate_key, currency_code, market, rate, effective_at, fetched_at,
               previous_rate, change_percentage, source, updated_at
        FROM exchange_rates
        ORDER BY rate_key
        """
    )
    return [dict(row._mapping) if hasattr(row, "_mapping") else dict(row) for row in rows]


def _serialize_decimal(value: Any) -> str | None:
    decimal_value = _decimal_or_none(value)
    if decimal_value is None:
        return None
    return format(decimal_value.normalize(), "f")


def _serialize_datetime(value: Any) -> str:
    parsed = _datetime_or_none(value)
    if not parsed:
        return str(value or "")
    return parsed.astimezone(UTC).isoformat()


def serialize_rate_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "rate_key": row["rate_key"],
        "currency_code": row["currency_code"],
        "market": row["market"],
        "rate": _serialize_decimal(row["rate"]),
        "effective_at": _serialize_datetime(row["effective_at"]),
        "fetched_at": _serialize_datetime(row["fetched_at"]),
        "previous_rate": _serialize_decimal(row.get("previous_rate")),
        "change_percentage": _serialize_decimal(row.get("change_percentage")),
        "source": row["source"],
        "updated_at": _serialize_datetime(row["updated_at"]),
        "freshness": rate_freshness(row),
    }


def rate_freshness(row: dict[str, Any]) -> dict[str, Any]:
    fetched_at = _datetime_or_none(row.get("fetched_at"))
    if not fetched_at:
        return {"stale": True, "reason": "missing_fetched_at"}
    if row.get("rate_key") != "usdt_binance":
        return {"stale": False, "reason": "bcv_effective_date_can_remain_current"}
    threshold = timedelta(minutes=max(1, get_config().dolarvzla_usdt_stale_minutes))
    stale = datetime.now(UTC) - fetched_at > threshold
    return {
        "stale": stale,
        "threshold_minutes": int(threshold.total_seconds() // 60),
        "reason": "fetched_at_exceeds_threshold" if stale else "fresh",
    }


def build_store_rate_settings(rows: list[dict[str, Any]], *, synced_at: datetime | None = None) -> dict[str, str]:
    by_key = {row["rate_key"]: row for row in rows}
    settings: dict[str, str] = {}
    mapping = {
        "usd_bcv": ("exchange_rate_usd_bcv", "exchange_rate_usd_bcv_effective_at"),
        "eur_bcv": ("exchange_rate_eur_bcv", "exchange_rate_eur_bcv_effective_at"),
        "usdt_binance": ("exchange_rate_usdt_binance", "exchange_rate_usdt_binance_effective_at"),
    }
    for rate_key, (value_key, effective_key) in mapping.items():
        row = by_key.get(rate_key)
        if not row:
            continue
        rate = _serialize_decimal(row["rate"])
        if rate is None:
            continue
        settings[value_key] = rate
        settings[effective_key] = _serialize_datetime(row["effective_at"])
    if settings:
        sync_time = synced_at or datetime.now(UTC)
        settings["exchange_rates_last_synced_at"] = sync_time.astimezone(UTC).isoformat()
    return settings


async def refresh_exchange_rates(
    *,
    include_bcv: bool = True,
    include_usdt: bool = True,
    client: DolarVzlaClient | None = None,
) -> dict[str, Any]:
    if _refresh_lock.locked():
        return {"status": "skipped", "reason": "refresh_already_running", "sources": {}}

    async with _refresh_lock:
        client = client or DolarVzlaClient.from_config()
        result: dict[str, Any] = {"status": "ok", "sources": {}}

        if include_bcv:
            try:
                bcv_rates = await client.fetch_bcv_rates()
                changed = []
                for rate in bcv_rates:
                    changed.append({"rate_key": rate.rate_key, "changed": await upsert_exchange_rate(rate)})
                result["sources"]["bcv"] = {"ok": True, "rates": changed}
            except Exception:
                logger.warning(
                    "DolarVZLA rate refresh failed",
                    extra={"source": "DolarVZLA BCV CDN", "rate_key": "usd_bcv,eur_bcv", "success": False},
                )
                result["sources"]["bcv"] = {"ok": False, "error": "fetch_failed"}

        if include_usdt:
            try:
                usdt_rate = await client.fetch_usdt_rate()
                changed = await upsert_exchange_rate(usdt_rate)
                result["sources"]["usdt"] = {"ok": True, "rates": [{"rate_key": usdt_rate.rate_key, "changed": changed}]}
            except Exception:
                logger.warning(
                    "DolarVZLA rate refresh failed",
                    extra={"source": "DolarVZLA USDT Binance", "rate_key": "usdt_binance", "success": False},
                )
                result["sources"]["usdt"] = {"ok": False, "error": "fetch_failed"}

        if not any(source.get("ok") for source in result["sources"].values()):
            result["status"] = "failed"
        return result


def as_public_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    serialized = [serialize_rate_row(row) for row in rows]
    return {
        "rates": serialized,
        "available_rate_keys": [row["rate_key"] for row in serialized],
        "missing_rate_keys": [key for key in RATE_KEYS if key not in {row["rate_key"] for row in serialized}],
    }


def normalized_rate_asdict(rate: NormalizedExchangeRate) -> dict[str, Any]:
    """Test helper that preserves Decimal/datetime objects for assertions."""
    return asdict(rate)
