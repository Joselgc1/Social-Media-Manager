"""Master-side exchange-rate persistence, refresh, freshness, and store sync payloads."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app import db
from app.config import get_config
from app.stores.dolarvzla import (
    DolarVzlaClient,
    NormalizedExchangeRate,
)

logger = logging.getLogger(__name__)

RATE_KEYS = ("usd_bcv", "eur_bcv", "usdt_binance")
_refresh_lock = asyncio.Lock()


class ExchangeRateValidationError(ValueError):
    """Raised when normalized provider data is incomplete or unsafe to persist."""


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return decimal_value if decimal_value.is_finite() else None


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
    validate_exchange_rate(rate)
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


def _serialize_datetime_or_none(value: Any) -> str | None:
    parsed = _datetime_or_none(value)
    return parsed.astimezone(UTC).isoformat() if parsed else None


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
        "usd_bcv": (
            "exchange_rate_usd_bcv",
            "exchange_rate_usd_bcv_effective_at",
            "exchange_rate_usd_bcv_fetched_at",
            "exchange_rate_usd_bcv_source",
        ),
        "eur_bcv": (
            "exchange_rate_eur_bcv",
            "exchange_rate_eur_bcv_effective_at",
            "exchange_rate_eur_bcv_fetched_at",
            "exchange_rate_eur_bcv_source",
        ),
        "usdt_binance": (
            "exchange_rate_usdt_binance",
            "exchange_rate_usdt_binance_effective_at",
            "exchange_rate_usdt_binance_fetched_at",
            "exchange_rate_usdt_binance_source",
        ),
    }
    for rate_key, (value_key, effective_key, fetched_key, source_key) in mapping.items():
        row = by_key.get(rate_key)
        if not row:
            continue
        rate = _serialize_decimal(row["rate"])
        effective_at = _serialize_datetime_or_none(row.get("effective_at"))
        fetched_at = _serialize_datetime_or_none(row.get("fetched_at"))
        source = str(row.get("source") or "").strip()
        if rate is None or Decimal(rate) <= 0 or not effective_at or not fetched_at or not source:
            continue
        settings[value_key] = rate
        settings[effective_key] = effective_at
        settings[fetched_key] = fetched_at
        settings[source_key] = source
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
        result: dict[str, Any] = {"status": "ok", "sources": {}, "successful_rate_keys": []}

        if include_bcv:
            try:
                bcv_rates = await client.fetch_bcv_rates()
                bcv_rates = validate_exchange_rate_source(
                    "bcv",
                    bcv_rates,
                    required_rate_keys={"usd_bcv", "eur_bcv"},
                )
                changed = []
                for rate in bcv_rates:
                    changed.append({"rate_key": rate.rate_key, "changed": await upsert_exchange_rate(rate)})
                result["sources"]["bcv"] = {"ok": True, "rates": changed}
                result["successful_rate_keys"].extend(rate.rate_key for rate in bcv_rates)
            except ExchangeRateValidationError:
                logger.warning(
                    "DolarVZLA rate refresh failed",
                    extra={
                        "source": "DolarVZLA BCV CDN",
                        "rate_key": "usd_bcv,eur_bcv",
                        "success": False,
                        "reason": "validation_failed",
                    },
                )
                result["sources"]["bcv"] = {"ok": False, "error": "validation_failed"}
            except Exception:
                logger.warning(
                    "DolarVZLA rate refresh failed",
                    extra={"source": "DolarVZLA BCV CDN", "rate_key": "usd_bcv,eur_bcv", "success": False},
                )
                result["sources"]["bcv"] = {"ok": False, "error": "fetch_failed"}

        if include_usdt:
            try:
                usdt_rate = await client.fetch_usdt_rate()
                usdt_rate = validate_exchange_rate_source(
                    "usdt",
                    [usdt_rate],
                    required_rate_keys={"usdt_binance"},
                )[0]
                changed = await upsert_exchange_rate(usdt_rate)
                result["sources"]["usdt"] = {"ok": True, "rates": [{"rate_key": usdt_rate.rate_key, "changed": changed}]}
                result["successful_rate_keys"].append(usdt_rate.rate_key)
            except ExchangeRateValidationError:
                logger.warning(
                    "USDT.com.ve rate refresh failed",
                    extra={
                        "source": "USDT.com.ve Binance",
                        "rate_key": "usdt_binance",
                        "success": False,
                        "reason": "validation_failed",
                    },
                )
                result["sources"]["usdt"] = {"ok": False, "error": "validation_failed"}
            except Exception:
                logger.warning(
                    "USDT.com.ve rate refresh failed",
                    extra={"source": "USDT.com.ve Binance", "rate_key": "usdt_binance", "success": False},
                )
                result["sources"]["usdt"] = {"ok": False, "error": "fetch_failed"}

        if not any(source.get("ok") for source in result["sources"].values()):
            result["status"] = "failed"
        return result


def validate_exchange_rate_source(
    source_name: str,
    rates: list[NormalizedExchangeRate],
    *,
    required_rate_keys: set[str],
) -> list[NormalizedExchangeRate]:
    if not rates:
        raise ExchangeRateValidationError(f"{source_name}_source_returned_no_rates")
    by_key = {rate.rate_key: rate for rate in rates if isinstance(rate, NormalizedExchangeRate)}
    if set(by_key) != required_rate_keys:
        raise ExchangeRateValidationError(f"{source_name}_source_returned_incomplete_rates")
    for rate in by_key.values():
        validate_exchange_rate(rate)
    return [by_key[rate_key] for rate_key in RATE_KEYS if rate_key in by_key]


def validate_exchange_rate(rate: NormalizedExchangeRate) -> None:
    if not isinstance(rate, NormalizedExchangeRate):
        raise ExchangeRateValidationError("rate_payload_type_invalid")
    if rate.rate_key not in RATE_KEYS:
        raise ExchangeRateValidationError("rate_key_invalid")
    if not str(rate.currency_code or "").strip() or not str(rate.market or "").strip():
        raise ExchangeRateValidationError("rate_metadata_missing")
    if _decimal_or_none(rate.rate) is None or _decimal_or_none(rate.rate) <= 0:
        raise ExchangeRateValidationError("rate_value_invalid")
    if _datetime_or_none(rate.effective_at) is None or _datetime_or_none(rate.fetched_at) is None:
        raise ExchangeRateValidationError("rate_timestamp_missing")
    if _decimal_or_none(rate.previous_rate) is None or _decimal_or_none(rate.previous_rate) <= 0:
        raise ExchangeRateValidationError("previous_rate_invalid")
    if _decimal_or_none(rate.change_percentage) is None:
        raise ExchangeRateValidationError("change_percentage_invalid")
    if not str(rate.source or "").strip():
        raise ExchangeRateValidationError("rate_source_missing")


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
