import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from app import db
from app.admin.settings import _validate_setting_value
from app.ai import engine
from app.exchange_rates import build_exchange_rate_reply, parse_decimal
from app.runtime_settings import (
    PROVIDER_EXCHANGE_RATE_SETTING_KEYS,
    RUNTIME_SETTING_DEFAULTS,
    SYNCABLE_RUNTIME_SETTING_KEYS,
)
from fastapi import HTTPException


def _settings(**overrides):
    settings = {
        "exchange_rate_reference": "usd_bcv",
        "manual_exchange_rate": "760",
        "exchange_rate_usd_bcv": "736.9339",
        "exchange_rate_usd_bcv_effective_at": "2026-07-20T00:00:00+00:00",
        "exchange_rate_eur_bcv": "843.19976838",
        "exchange_rate_eur_bcv_effective_at": "2026-07-20T00:00:00+00:00",
        "exchange_rate_usdt_binance": "780.50",
        "exchange_rate_usdt_binance_effective_at": "2026-07-20T00:00:00+00:00",
        "exchange_rates_last_synced_at": "2026-07-20T12:00:00+00:00",
    }
    settings.update(overrides)
    return settings


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("usd_bcv", "La tasa del dólar BCV que usamos actualmente es 736,93 Bs por USD."),
        ("eur_bcv", "La tasa del euro BCV que usamos actualmente es 843,20 Bs por EUR."),
        ("usdt_binance", "La tasa USDT de Binance que usamos actualmente es 780,50 Bs por USDT."),
        ("manual", "La tasa que usamos actualmente es 760 Bs por USD."),
    ],
)
def test_each_exchange_rate_reference_returns_correct_label_and_unit(reference, expected):
    assert build_exchange_rate_reply(_settings(exchange_rate_reference=reference)) == expected


def test_selected_provider_rate_is_used_by_deterministic_reply():
    reply = engine._exchange_rate_reply(_settings(exchange_rate_reference="eur_bcv"))

    assert reply == "La tasa del euro BCV que usamos actualmente es 843,20 Bs por EUR."


def test_explicit_comparison_request_returns_all_available_provider_rates():
    reply = engine._exchange_rate_reply(_settings(exchange_rate_reference="usd_bcv"), "Compárame las tasas BCV y Binance")

    assert "Dólar BCV: 736,93 Bs por USD" in reply
    assert "Euro BCV: 843,20 Bs por EUR" in reply
    assert "USDT Binance: 780,50 Bs por USDT" in reply
    assert "referencia configurada para pagos es Dólar BCV" in reply


def test_selected_unavailable_rate_does_not_invent_value():
    reply = build_exchange_rate_reply(_settings(exchange_rate_reference="usdt_binance", exchange_rate_usdt_binance=""))

    assert "temporalmente no disponible" in reply
    assert "780" not in reply


def test_decimal_parser_returns_decimal_not_float():
    value = parse_decimal("736,93 Bs/USD")

    assert str(value) == "736.93"
    assert not isinstance(value, float)


@pytest.mark.asyncio
async def test_accepted_exchange_rate_is_preserved_during_migration(monkeypatch):
    async def fake_fetch_one(query, values=None):
        key = (values or {}).get("key")
        if key == "accepted_exchange_rate":
            return {"value": json.dumps("40,25 Bs/USD")}
        if key == "manual_exchange_rate":
            return None
        if "exchange_rate_reference" in query:
            return None
        return None

    execute = AsyncMock()
    monkeypatch.setattr(db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(db, "execute", execute)

    await db._ensure_exchange_rate_settings()

    executed = [call.args[1] if len(call.args) > 1 else call.kwargs.get("values") for call in execute.call_args_list]
    assert {item["key"] for item in executed if item and "key" in item} == {"manual_exchange_rate"}
    assert any(item and item.get("val") == json.dumps("40,25 Bs/USD") for item in executed)
    assert any(item and item.get("val") == json.dumps("manual") for item in executed)


def test_store_dashboard_cannot_edit_provider_supplied_rate_values():
    with pytest.raises(HTTPException):
        _validate_setting_value("exchange_rate_usd_bcv", "1", {})


def test_exchange_rate_reference_and_manual_rate_are_store_editable():
    assert _validate_setting_value("exchange_rate_reference", "EUR_BCV", {}) == "eur_bcv"
    assert _validate_setting_value("manual_exchange_rate", "760,50", {}) == "760.5"


def test_store_phone_number_is_store_editable_and_normalized():
    assert _validate_setting_value("store_phone_number", "  +58   412-1234567  ", {}) == "+58 412-1234567"
    assert _validate_setting_value("store_phone_number", "", {}) == ""
    with pytest.raises(HTTPException):
        _validate_setting_value("store_phone_number", "WhatsApp me", {})


def test_master_and_store_runtime_settings_remain_aligned():
    master_runtime_path = Path(__file__).resolve().parents[2] / "master" / "app" / "stores" / "runtime_settings.py"
    spec = importlib.util.spec_from_file_location("master_runtime_settings_for_test", master_runtime_path)
    master_runtime = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(master_runtime)

    assert set(master_runtime.DEFAULT_RUNTIME_SETTINGS).issubset(set(RUNTIME_SETTING_DEFAULTS))
    assert SYNCABLE_RUNTIME_SETTING_KEYS.issubset(set(RUNTIME_SETTING_DEFAULTS))
    assert master_runtime.SYNCABLE_RUNTIME_SETTING_KEYS.issubset(set(master_runtime.DEFAULT_RUNTIME_SETTINGS))
    assert PROVIDER_EXCHANGE_RATE_SETTING_KEYS == master_runtime.PROVIDER_EXCHANGE_RATE_SETTING_KEYS
    assert SYNCABLE_RUNTIME_SETTING_KEYS == master_runtime.SYNCABLE_RUNTIME_SETTING_KEYS
