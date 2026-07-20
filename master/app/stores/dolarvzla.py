"""DolarVZLA HTTP client and provider-specific response normalization."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

import httpx

from app.config import get_config

BCV_CURRENT_URL = "https://rates.dolarvzla.com/bcv/current.json"
USDT_EXCHANGE_RATE_URL = "https://api.dolarvzla.com/public/usdt/exchange-rate"
USDT_AUTH_HEADER = "x-dolarvzla-key"


@dataclass(frozen=True)
class NormalizedExchangeRate:
    rate_key: str
    currency_code: str
    market: str
    rate: Decimal
    effective_at: datetime
    fetched_at: datetime
    previous_rate: Decimal | None
    change_percentage: Decimal | None
    source: str


class DolarVzlaClientError(RuntimeError):
    """Raised when DolarVZLA data cannot be fetched or parsed."""


class DolarVzlaAuthError(DolarVzlaClientError):
    """Raised when DolarVZLA rejects the configured API key."""


class DolarVzlaConfigurationError(DolarVzlaClientError):
    """Raised when DolarVZLA credentials are missing."""


def _decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value)


def _effective_at(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise DolarVzlaClientError("DolarVZLA response is missing rate date")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError as exc:
        try:
            return datetime.combine(date.fromisoformat(text), time.min, tzinfo=UTC)
        except ValueError:
            raise DolarVzlaClientError(f"Invalid DolarVZLA rate date: {text}") from exc


def _fetched_at(value: datetime | None) -> datetime:
    now = value or datetime.now(UTC)
    return now if now.tzinfo else now.replace(tzinfo=UTC)


def parse_bcv_response(payload: dict[str, Any], *, fetched_at: datetime | None = None) -> list[NormalizedExchangeRate]:
    current = payload.get("current") or {}
    previous = payload.get("previous") or {}
    change = payload.get("changePercentage") or {}
    effective = _effective_at(current.get("date"))
    fetched = _fetched_at(fetched_at)

    return [
        NormalizedExchangeRate(
            rate_key="usd_bcv",
            currency_code="USD",
            market="bcv",
            rate=_decimal(current["usd"]),
            effective_at=effective,
            fetched_at=fetched,
            previous_rate=_optional_decimal(previous.get("usd")),
            change_percentage=_optional_decimal(change.get("usd")),
            source=BCV_CURRENT_URL,
        ),
        NormalizedExchangeRate(
            rate_key="eur_bcv",
            currency_code="EUR",
            market="bcv",
            rate=_decimal(current["eur"]),
            effective_at=effective,
            fetched_at=fetched,
            previous_rate=_optional_decimal(previous.get("eur")),
            change_percentage=_optional_decimal(change.get("eur")),
            source=BCV_CURRENT_URL,
        ),
    ]


def parse_usdt_response(payload: dict[str, Any], *, fetched_at: datetime | None = None) -> NormalizedExchangeRate:
    current = payload.get("current") or {}
    previous = payload.get("previous") or {}
    change = payload.get("changePercentage") or {}
    return NormalizedExchangeRate(
        rate_key="usdt_binance",
        currency_code="USDT",
        market="binance",
        rate=_decimal(current["average"]),
        effective_at=_effective_at(current.get("date")),
        fetched_at=_fetched_at(fetched_at),
        previous_rate=_optional_decimal(previous.get("average")),
        change_percentage=_optional_decimal(change.get("average")),
        source=USDT_EXCHANGE_RATE_URL,
    )


class DolarVzlaClient:
    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float = 10.0,
        retries: int = 2,
        retry_backoff_seconds: float = 1.0,
    ):
        self.api_key = str(api_key or "").strip()
        self.timeout_seconds = timeout_seconds
        self.retries = max(0, retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)

    @classmethod
    def from_config(cls) -> DolarVzlaClient:
        config = get_config()
        return cls(
            api_key=config.dolarvzla_api_key,
            timeout_seconds=config.dolarvzla_http_timeout_seconds,
            retries=config.dolarvzla_http_retries,
            retry_backoff_seconds=config.dolarvzla_retry_backoff_seconds,
        )

    async def fetch_bcv_rates(self) -> list[NormalizedExchangeRate]:
        payload = await self._get_json(BCV_CURRENT_URL)
        return parse_bcv_response(payload)

    async def fetch_usdt_rate(self) -> NormalizedExchangeRate:
        if not self.api_key:
            raise DolarVzlaConfigurationError("DOLARVZLA_API_KEY is not configured in the master service")
        payload = await self._get_json(USDT_EXCHANGE_RATE_URL, headers={USDT_AUTH_HEADER: self.api_key})
        return parse_usdt_response(payload)

    async def _get_json(self, url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
        last_error: Exception | None = None
        timeout = httpx.Timeout(self.timeout_seconds)
        safe_headers = headers or {}
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for attempt in range(self.retries + 1):
                try:
                    response = await client.get(url, headers=safe_headers)
                    response.raise_for_status()
                    return json.loads(response.text, parse_float=Decimal, parse_int=Decimal)
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    if exc.response.status_code in {401, 403}:
                        raise DolarVzlaAuthError("DolarVZLA API key is missing, invalid, or expired") from exc
                    if attempt < self.retries:
                        await asyncio.sleep(self.retry_backoff_seconds * (2**attempt))
                except (httpx.HTTPError, json.JSONDecodeError) as exc:
                    last_error = exc
                    if attempt < self.retries:
                        await asyncio.sleep(self.retry_backoff_seconds * (2**attempt))
        raise DolarVzlaClientError(f"DolarVZLA request failed for {url}") from last_error
