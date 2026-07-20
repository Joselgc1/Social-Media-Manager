from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest


def _rate(rate_key="usd_bcv", rate="736.9339"):
    from app.stores.dolarvzla import NormalizedExchangeRate

    currency = {"usd_bcv": "USD", "eur_bcv": "EUR", "usdt_binance": "USDT"}[rate_key]
    market = "binance" if rate_key == "usdt_binance" else "bcv"
    source = "DolarVZLA USDT Binance" if rate_key == "usdt_binance" else "DolarVZLA BCV CDN"
    return NormalizedExchangeRate(
        rate_key=rate_key,
        currency_code=currency,
        market=market,
        rate=Decimal(rate),
        effective_at=datetime(2026, 7, 20, tzinfo=UTC),
        fetched_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
        previous_rate=Decimal("700.00"),
        change_percentage=Decimal("1.25"),
        source=source,
    )


def test_bcv_usd_and_eur_response_is_parsed_with_decimal_values():
    from app.stores.dolarvzla import parse_bcv_response

    rates = parse_bcv_response(
        {
            "current": {"usd": "736.9339", "eur": "843.19976838", "date": "2026-07-20"},
            "previous": {"usd": "732.4787", "eur": "838.0655046", "date": "2026-07-17"},
            "changePercentage": {"usd": "0.6082361166270078", "eur": "0.6126327538621817"},
        },
        fetched_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
    )

    assert [rate.rate_key for rate in rates] == ["usd_bcv", "eur_bcv"]
    assert rates[0].rate == Decimal("736.9339")
    assert rates[1].rate == Decimal("843.19976838")
    assert rates[0].previous_rate == Decimal("732.4787")
    assert rates[1].change_percentage == Decimal("0.6126327538621817")
    assert all(not isinstance(rate.rate, float) for rate in rates)


def test_usdt_response_follows_current_openapi_shape_and_uses_average():
    from app.stores.dolarvzla import USDT_AUTH_HEADER, USDT_EXCHANGE_RATE_URL, parse_usdt_response

    rate = parse_usdt_response(
        {
            "current": {"buy": "779.10", "sell": "781.90", "average": "780.50", "date": "2026-07-20"},
            "previous": {"buy": "770.00", "sell": "774.00", "average": "772.00", "date": "2026-07-19"},
            "changePercentage": {"buy": "1.18", "sell": "1.02", "average": "1.10"},
        },
        fetched_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
    )

    assert USDT_EXCHANGE_RATE_URL == "https://api.dolarvzla.com/public/usdt/exchange-rate"
    assert USDT_AUTH_HEADER == "x-dolarvzla-key"
    assert rate.rate_key == "usdt_binance"
    assert rate.currency_code == "USDT"
    assert rate.market == "binance"
    assert rate.rate == Decimal("780.50")
    assert rate.previous_rate == Decimal("772.00")
    assert rate.change_percentage == Decimal("1.10")
    assert not isinstance(rate.rate, float)


@pytest.mark.asyncio
async def test_successful_refresh_upserts_all_available_rates(monkeypatch):
    from app.stores import exchange_rates

    class FakeClient:
        async def fetch_bcv_rates(self):
            return [_rate("usd_bcv", "736.9339"), _rate("eur_bcv", "843.19976838")]

        async def fetch_usdt_rate(self):
            return _rate("usdt_binance", "780.50")

    execute = AsyncMock()
    monkeypatch.setattr(exchange_rates.db, "fetch_one", AsyncMock(return_value=None))
    monkeypatch.setattr(exchange_rates.db, "execute", execute)

    result = await exchange_rates.refresh_exchange_rates(client=FakeClient())

    assert result["status"] == "ok"
    assert result["sources"]["bcv"]["ok"] is True
    assert result["sources"]["usdt"]["ok"] is True
    assert execute.await_count == 3
    persisted = [call.args[1] for call in execute.call_args_list]
    assert {item["rate_key"] for item in persisted} == {"usd_bcv", "eur_bcv", "usdt_binance"}
    assert all(isinstance(item["rate"], Decimal) for item in persisted)


@pytest.mark.asyncio
async def test_partial_provider_failure_does_not_delete_previous_values(monkeypatch):
    from app.stores import exchange_rates

    class FakeClient:
        async def fetch_bcv_rates(self):
            raise RuntimeError("bcv unavailable")

        async def fetch_usdt_rate(self):
            return _rate("usdt_binance", "780.50")

    execute = AsyncMock()
    monkeypatch.setattr(exchange_rates.db, "fetch_one", AsyncMock(return_value=None))
    monkeypatch.setattr(exchange_rates.db, "execute", execute)

    result = await exchange_rates.refresh_exchange_rates(client=FakeClient())

    assert result["status"] == "ok"
    assert result["sources"]["bcv"] == {"ok": False, "error": "fetch_failed"}
    assert result["sources"]["usdt"]["ok"] is True
    persisted = [call.args[1] for call in execute.call_args_list]
    assert [item["rate_key"] for item in persisted] == ["usdt_binance"]


@pytest.mark.asyncio
async def test_api_keys_are_not_logged_or_propagated_to_store_settings(monkeypatch, caplog):
    from app.stores import exchange_rates

    secret = "dvzla-secret-should-not-leak"

    class FakeClient:
        async def fetch_bcv_rates(self):
            return []

        async def fetch_usdt_rate(self):
            raise RuntimeError(secret)

    monkeypatch.setattr(exchange_rates.db, "fetch_one", AsyncMock(return_value=None))
    monkeypatch.setattr(exchange_rates.db, "execute", AsyncMock())

    with caplog.at_level("WARNING"):
        result = await exchange_rates.refresh_exchange_rates(include_bcv=False, include_usdt=True, client=FakeClient())

    assert secret not in caplog.text
    assert secret not in str(result)

    settings = exchange_rates.build_store_rate_settings([
        {
            "rate_key": "usdt_binance",
            "rate": Decimal("780.50"),
            "effective_at": datetime(2026, 7, 20, tzinfo=UTC),
            "fetched_at": datetime(2026, 7, 20, 12, tzinfo=UTC),
        }
    ])
    assert "DOLARVZLA_API_KEY" not in settings
    assert secret not in str(settings)


def test_usdt_freshness_uses_fetched_at_threshold(monkeypatch):
    from app.stores import exchange_rates

    monkeypatch.setattr(
        exchange_rates,
        "get_config",
        lambda: type("Config", (), {"dolarvzla_usdt_stale_minutes": 60})(),
    )

    row = {
        "rate_key": "usdt_binance",
        "fetched_at": datetime.now(UTC) - timedelta(minutes=90),
    }

    assert exchange_rates.rate_freshness(row)["stale"] is True
