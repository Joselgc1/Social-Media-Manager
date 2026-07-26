import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _rate(rate_key="usd_bcv", rate="736.9339"):
    from app.stores.dolarvzla import BCV_CURRENT_URL, USDT_EXCHANGE_RATE_URL, NormalizedExchangeRate

    currency = {"usd_bcv": "USD", "eur_bcv": "EUR", "usdt_binance": "USDT"}[rate_key]
    market = "binance" if rate_key == "usdt_binance" else "bcv"
    source = USDT_EXCHANGE_RATE_URL if rate_key == "usdt_binance" else BCV_CURRENT_URL
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


def exchange_rate_service_row(rate_key="usd_bcv", rate="736.9339"):
    normalized = _rate(rate_key, rate)
    return {
        "rate_key": normalized.rate_key,
        "currency_code": normalized.currency_code,
        "market": normalized.market,
        "rate": normalized.rate,
        "effective_at": normalized.effective_at,
        "fetched_at": normalized.fetched_at,
        "previous_rate": normalized.previous_rate,
        "change_percentage": normalized.change_percentage,
        "source": normalized.source,
        "updated_at": normalized.fetched_at,
    }


def test_bcv_usd_and_eur_response_is_parsed_with_decimal_values():
    from app.stores.dolarvzla import BCV_CURRENT_URL, parse_bcv_response

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
    assert all(rate.source == BCV_CURRENT_URL for rate in rates)
    assert all(not isinstance(rate.rate, float) for rate in rates)


def test_usdt_response_uses_public_binance_buy_rate_and_capture_timestamp():
    from app.stores.dolarvzla import USDT_EXCHANGE_RATE_URL, parse_usdt_response

    rate = parse_usdt_response(
        {
            "success": True,
            "data": {
                "binance": {"buy_rate": "779.10", "sell_rate": "781.90", "spread": "0.36"},
                "captured_at": "2026-07-20T21:00:03.932Z",
            },
        },
        fetched_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
    )

    assert USDT_EXCHANGE_RATE_URL == "https://www.usdt.com.ve/api/v1/rates/current"
    assert rate.rate_key == "usdt_binance"
    assert rate.currency_code == "USDT"
    assert rate.market == "binance"
    assert rate.rate == Decimal("779.10")
    assert rate.effective_at == datetime(2026, 7, 20, 21, 0, 3, 932000, tzinfo=UTC)
    assert rate.previous_rate == Decimal("779.10")
    assert rate.change_percentage == Decimal("0")
    assert rate.source == USDT_EXCHANGE_RATE_URL
    assert not isinstance(rate.rate, float)


@pytest.mark.parametrize(
    "payload",
    [
        {"current": {"usd": "736.9339", "date": "2026-07-20"}},
        {
            "current": {"usd": "0", "eur": "843.19976838", "date": "2026-07-20"},
            "previous": {"usd": "732.4787", "eur": "838.0655046"},
            "changePercentage": {"usd": "0.6", "eur": "0.6"},
        },
        {
            "current": {"usd": "not-a-rate", "eur": "843.19976838", "date": "2026-07-20"},
            "previous": {"usd": "732.4787", "eur": "838.0655046"},
            "changePercentage": {"usd": "0.6", "eur": "0.6"},
        },
    ],
)
def test_bcv_malformed_incomplete_or_zero_responses_are_rejected(payload):
    from app.stores.dolarvzla import DolarVzlaClientError, parse_bcv_response

    with pytest.raises(DolarVzlaClientError):
        parse_bcv_response(payload, fetched_at=datetime(2026, 7, 20, 12, tzinfo=UTC))


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": {}},
        {"data": {"binance": {}, "captured_at": "2026-07-20T21:00:03.932Z"}},
        {
            "data": {
                "binance": {"buy_rate": "0"},
                "captured_at": "2026-07-20T21:00:03.932Z",
            },
        },
        {
            "data": {
                "binance": {"buy_rate": "NaN"},
                "captured_at": "2026-07-20T21:00:03.932Z",
            },
        },
    ],
)
def test_usdt_malformed_incomplete_or_zero_responses_are_rejected(payload):
    from app.stores.dolarvzla import DolarVzlaClientError, parse_usdt_response

    with pytest.raises(DolarVzlaClientError):
        parse_usdt_response(payload, fetched_at=datetime(2026, 7, 20, 12, tzinfo=UTC))


@pytest.mark.asyncio
async def test_usdt_fetch_uses_public_endpoint_without_authentication(monkeypatch):
    from app.stores.dolarvzla import USDT_EXCHANGE_RATE_URL, DolarVzlaClient

    captured = {}

    async def fake_get_json(self, url, *, headers=None):
        captured["url"] = url
        captured["headers"] = headers
        return {
            "data": {
                "binance": {"buy_rate": "779.10"},
                "captured_at": "2026-07-20T21:00:03.932Z",
            },
        }

    monkeypatch.setattr(DolarVzlaClient, "_get_json", fake_get_json)

    await DolarVzlaClient().fetch_usdt_rate()

    assert captured["url"] == USDT_EXCHANGE_RATE_URL
    assert captured["headers"] is None


@pytest.mark.asyncio
async def test_usdt_http_failure_is_reported_after_retries(monkeypatch):
    from app.stores import dolarvzla

    requests = []

    class FailingAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, *, headers):
            requests.append((url, headers))
            return httpx.Response(503, request=httpx.Request("GET", url))

    monkeypatch.setattr(dolarvzla.httpx, "AsyncClient", lambda **kwargs: FailingAsyncClient())

    with pytest.raises(dolarvzla.DolarVzlaClientError):
        await dolarvzla.DolarVzlaClient(retries=0).fetch_usdt_rate()

    assert requests == [(dolarvzla.USDT_EXCHANGE_RATE_URL, {})]


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
    assert result["successful_rate_keys"] == ["usdt_binance"]
    persisted = [call.args[1] for call in execute.call_args_list]
    assert [item["rate_key"] for item in persisted] == ["usdt_binance"]


@pytest.mark.asyncio
async def test_total_refresh_failure_does_not_write_any_exchange_rate(monkeypatch):
    from app.stores import exchange_rates

    class FakeClient:
        async def fetch_bcv_rates(self):
            raise RuntimeError("bcv unavailable")

        async def fetch_usdt_rate(self):
            raise RuntimeError("usdt unavailable")

    execute = AsyncMock()
    monkeypatch.setattr(exchange_rates.db, "fetch_one", AsyncMock(return_value={"rate": Decimal("700")}))
    monkeypatch.setattr(exchange_rates.db, "execute", execute)

    result = await exchange_rates.refresh_exchange_rates(client=FakeClient())

    assert result["status"] == "failed"
    assert result["sources"]["bcv"] == {"ok": False, "error": "fetch_failed"}
    assert result["sources"]["usdt"] == {"ok": False, "error": "fetch_failed"}
    assert result["successful_rate_keys"] == []
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_or_incomplete_normalized_rates_are_not_persisted(monkeypatch):
    from app.stores import exchange_rates

    class FakeClient:
        async def fetch_bcv_rates(self):
            return [_rate("usd_bcv", "736.9339")]

        async def fetch_usdt_rate(self):
            return replace(_rate("usdt_binance", "780.50"), rate=Decimal("0"))

    execute = AsyncMock()
    monkeypatch.setattr(exchange_rates.db, "fetch_one", AsyncMock(return_value=None))
    monkeypatch.setattr(exchange_rates.db, "execute", execute)

    result = await exchange_rates.refresh_exchange_rates(client=FakeClient())

    assert result["status"] == "failed"
    assert result["sources"]["bcv"] == {"ok": False, "error": "validation_failed"}
    assert result["sources"]["usdt"] == {"ok": False, "error": "validation_failed"}
    execute.assert_not_awaited()


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
    assert secret not in str(settings)


def test_store_rate_settings_include_source_and_fetch_metadata():
    from app.stores import exchange_rates

    settings = exchange_rates.build_store_rate_settings([{
        "rate_key": "usd_bcv",
        "rate": Decimal("736.9339"),
        "effective_at": datetime(2026, 7, 20, tzinfo=UTC),
        "fetched_at": datetime(2026, 7, 20, 12, 30, tzinfo=UTC),
        "source": "https://rates.dolarvzla.com/bcv/current.json",
    }])

    assert settings["exchange_rate_usd_bcv"] == "736.9339"
    assert settings["exchange_rate_usd_bcv_effective_at"] == "2026-07-20T00:00:00+00:00"
    assert settings["exchange_rate_usd_bcv_fetched_at"] == "2026-07-20T12:30:00+00:00"
    assert settings["exchange_rate_usd_bcv_source"] == "https://rates.dolarvzla.com/bcv/current.json"
    assert settings["exchange_rates_last_synced_at"]


def test_store_rate_settings_omit_invalid_rows_instead_of_emptying_values():
    from app.stores import exchange_rates

    settings = exchange_rates.build_store_rate_settings([
        {
            "rate_key": "usd_bcv",
            "rate": Decimal("0"),
            "effective_at": datetime(2026, 7, 20, tzinfo=UTC),
            "fetched_at": datetime(2026, 7, 20, 12, tzinfo=UTC),
            "source": "https://rates.dolarvzla.com/bcv/current.json",
        },
        {
            "rate_key": "usdt_binance",
            "rate": Decimal("780.50"),
            "effective_at": datetime(2026, 7, 20, tzinfo=UTC),
            "fetched_at": None,
            "source": "https://www.usdt.com.ve/api/v1/rates/current",
        },
    ])

    assert settings == {}


@pytest.mark.asyncio
async def test_store_sync_after_partial_failure_only_writes_successful_source(monkeypatch):
    from app.stores import api

    store_db = SimpleNamespace(execute=AsyncMock(), transaction=lambda: _Tx())
    monkeypatch.setattr(api, "_get_store_row", AsyncMock(return_value={"id": "store-1", "name": "Store", "db_url_encrypted": "encrypted"}))
    monkeypatch.setattr(api, "decrypt", lambda value: "postgresql://store")
    monkeypatch.setattr(api, "_get_store_db", AsyncMock(return_value=store_db))
    monkeypatch.setattr(api, "_store_stats_semaphore", asyncio.Semaphore(1))
    rows = [
        exchange_rate_service_row("usd_bcv", "736.9339"),
        exchange_rate_service_row("usdt_binance", "780.50"),
    ]

    result = await api.sync_exchange_rates_to_store(
        "store-1",
        rows=rows,
        rate_keys={"usdt_binance"},
    )

    written_keys = [call.args[1]["key"] for call in store_db.execute.await_args_list]
    assert result["synced"] is True
    assert "exchange_rate_usdt_binance" in written_keys
    assert "exchange_rate_usd_bcv" not in written_keys
    assert all("usd_bcv" not in key for key in written_keys)


@pytest.mark.asyncio
async def test_store_sync_after_total_failure_writes_nothing(monkeypatch):
    from app.stores import api

    get_store_db = AsyncMock()
    monkeypatch.setattr(api, "_get_store_row", AsyncMock(return_value={"id": "store-1", "name": "Store", "db_url_encrypted": "encrypted"}))
    monkeypatch.setattr(api, "_get_store_db", get_store_db)

    result = await api.sync_exchange_rates_to_store(
        "store-1",
        rows=[exchange_rate_service_row("usd_bcv", "736.9339")],
        rate_keys=set(),
    )

    assert result["synced"] is False
    assert result["settings"] == []
    get_store_db.assert_not_awaited()


@pytest.mark.asyncio
async def test_later_successful_refresh_persists_replacement_after_retained_failure(monkeypatch):
    from app.stores import exchange_rates

    class FailingClient:
        async def fetch_bcv_rates(self):
            raise RuntimeError("bcv unavailable")

        async def fetch_usdt_rate(self):
            raise RuntimeError("usdt unavailable")

    class SuccessfulClient:
        async def fetch_bcv_rates(self):
            return [_rate("usd_bcv", "740.00"), _rate("eur_bcv", "845.00")]

        async def fetch_usdt_rate(self):
            return _rate("usdt_binance", "790.00")

    execute = AsyncMock()
    monkeypatch.setattr(exchange_rates.db, "fetch_one", AsyncMock(return_value=None))
    monkeypatch.setattr(exchange_rates.db, "execute", execute)

    failed = await exchange_rates.refresh_exchange_rates(client=FailingClient())
    assert failed["status"] == "failed"
    execute.assert_not_awaited()

    successful = await exchange_rates.refresh_exchange_rates(client=SuccessfulClient())
    assert successful["status"] == "ok"
    persisted = {call.args[1]["rate_key"]: call.args[1]["rate"] for call in execute.await_args_list}
    assert persisted == {
        "usd_bcv": Decimal("740.00"),
        "eur_bcv": Decimal("845.00"),
        "usdt_binance": Decimal("790.00"),
    }


def test_master_runtime_normalizes_store_phone_number():
    from app.stores.api import _normalize_runtime_fields

    normalized = _normalize_runtime_fields(
        {"store_phone_number": "  +58   412-1234567  "},
        {"llm_provider": "openai", "fallback_provider": "anthropic"},
    )

    assert normalized["store_phone_number"] == "+58 412-1234567"


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
