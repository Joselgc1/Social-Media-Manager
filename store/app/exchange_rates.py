"""Store-side exchange-rate selection and customer-facing formatting."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

DEFAULT_EXCHANGE_RATE_REFERENCE = "usd_bcv"
MANUAL_EXCHANGE_RATE_KEY = "manual_exchange_rate"
LEGACY_ACCEPTED_EXCHANGE_RATE_KEY = "accepted_exchange_rate"


@dataclass(frozen=True)
class ExchangeRateReference:
    key: str
    label: str
    customer_subject: str
    unit: str
    rate_setting: str
    effective_at_setting: str | None = None


EXCHANGE_RATE_REFERENCES = {
    "usd_bcv": ExchangeRateReference(
        key="usd_bcv",
        label="Dólar BCV",
        customer_subject="la tasa del dólar BCV",
        unit="USD",
        rate_setting="exchange_rate_usd_bcv",
        effective_at_setting="exchange_rate_usd_bcv_effective_at",
    ),
    "eur_bcv": ExchangeRateReference(
        key="eur_bcv",
        label="Euro BCV",
        customer_subject="la tasa del euro BCV",
        unit="EUR",
        rate_setting="exchange_rate_eur_bcv",
        effective_at_setting="exchange_rate_eur_bcv_effective_at",
    ),
    "usdt_binance": ExchangeRateReference(
        key="usdt_binance",
        label="USDT Binance",
        customer_subject="la tasa USDT de Binance",
        unit="USDT",
        rate_setting="exchange_rate_usdt_binance",
        effective_at_setting="exchange_rate_usdt_binance_effective_at",
    ),
    "manual": ExchangeRateReference(
        key="manual",
        label="Tasa manual",
        customer_subject="la tasa",
        unit="USD",
        rate_setting=MANUAL_EXCHANGE_RATE_KEY,
    ),
}

ALLOWED_EXCHANGE_RATE_REFERENCES = set(EXCHANGE_RATE_REFERENCES)
PROVIDER_EXCHANGE_RATE_SETTING_KEYS = {
    "exchange_rate_usd_bcv",
    "exchange_rate_usd_bcv_effective_at",
    "exchange_rate_usd_bcv_fetched_at",
    "exchange_rate_usd_bcv_source",
    "exchange_rate_eur_bcv",
    "exchange_rate_eur_bcv_effective_at",
    "exchange_rate_eur_bcv_fetched_at",
    "exchange_rate_eur_bcv_source",
    "exchange_rate_usdt_binance",
    "exchange_rate_usdt_binance_effective_at",
    "exchange_rate_usdt_binance_fetched_at",
    "exchange_rate_usdt_binance_source",
    "exchange_rates_last_synced_at",
}


def normalize_exchange_rate_reference(value: Any) -> str:
    reference = str(value or DEFAULT_EXCHANGE_RATE_REFERENCE).strip().lower()
    if reference not in ALLOWED_EXCHANGE_RATE_REFERENCES:
        return DEFAULT_EXCHANGE_RATE_REFERENCE
    return reference


def parse_decimal(value: Any) -> Decimal | None:
    """Parse a JSON-safe setting value into Decimal without using float arithmetic."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if not text:
        return None

    match = re.search(r"-?\d[\d.,]*", text)
    if not match:
        return None
    token = match.group(0)
    if "," in token and "." in token:
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif "," in token:
        token = token.replace(".", "").replace(",", ".")

    try:
        return Decimal(token)
    except InvalidOperation:
        return None


def normalize_rate_setting_value(value: Any) -> str:
    rate = parse_decimal(value)
    if rate is None:
        return ""
    if rate <= 0:
        raise ValueError("Exchange rate must be greater than 0.")
    return format(rate.normalize(), "f")


def format_rate_for_customer(rate: Decimal) -> str:
    rounded = rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    text = f"{rounded:,.2f}"
    text = text.replace(",", "_").replace(".", ",").replace("_", ".")
    if text.endswith(",00"):
        text = text[:-3]
    return text


def selected_exchange_rate(settings: dict[str, Any]) -> dict[str, Any]:
    reference_key = normalize_exchange_rate_reference(settings.get("exchange_rate_reference"))
    reference = EXCHANGE_RATE_REFERENCES[reference_key]
    raw_value = settings.get(reference.rate_setting)
    if reference_key == "manual" and not str(raw_value or "").strip():
        raw_value = settings.get(LEGACY_ACCEPTED_EXCHANGE_RATE_KEY)
    rate = parse_decimal(raw_value)
    effective_at = settings.get(reference.effective_at_setting) if reference.effective_at_setting else None
    return {
        "reference": reference,
        "reference_key": reference_key,
        "rate": rate,
        "effective_at": str(effective_at or "").strip(),
    }


def build_exchange_rate_reply(settings: dict[str, Any]) -> str:
    return build_selected_exchange_rate_reply(settings)


def build_customer_exchange_rate_reply(settings: dict[str, Any], message_text: str = "") -> str:
    if _asks_to_compare_rates(message_text):
        return build_exchange_rate_comparison_reply(settings)
    return build_selected_exchange_rate_reply(settings)


def build_selected_exchange_rate_reply(settings: dict[str, Any]) -> str:
    selected = selected_exchange_rate(settings)
    reference: ExchangeRateReference = selected["reference"]
    rate: Decimal | None = selected["rate"]
    if rate is None:
        if selected["reference_key"] == "manual":
            return "Ahorita no tengo una tasa manual configurada. La tienda puede confirmarla antes del pago."
        return (
            f"Ahorita {reference.customer_subject} está temporalmente no disponible. "
            "No quiero inventarte un valor; la tienda puede confirmarla antes del pago."
        )

    rate_text = format_rate_for_customer(rate)
    if selected["reference_key"] == "manual":
        return f"La tasa que usamos actualmente es {rate_text} Bs por USD."
    subject = reference.customer_subject[:1].upper() + reference.customer_subject[1:]
    return f"{subject} que usamos actualmente es {rate_text} Bs por {reference.unit}."


def build_exchange_rate_comparison_reply(settings: dict[str, Any]) -> str:
    lines = []
    for reference_key in ("usd_bcv", "eur_bcv", "usdt_binance"):
        reference = EXCHANGE_RATE_REFERENCES[reference_key]
        rate = parse_decimal(settings.get(reference.rate_setting))
        if rate is not None:
            lines.append(f"{reference.label}: {format_rate_for_customer(rate)} Bs por {reference.unit}")
    if not lines:
        return "Ahorita las tasas actuales están temporalmente no disponibles. No quiero inventarte valores."

    selected = selected_exchange_rate(settings)
    selected_reference: ExchangeRateReference = selected["reference"]
    selected_label = selected_reference.label if selected["reference_key"] != "manual" else "Tasa manual"
    return "Estas son las tasas disponibles: " + "; ".join(lines) + f". La referencia configurada para pagos es {selected_label}."


def _asks_to_compare_rates(message_text: str) -> bool:
    normalized = str(message_text or "").lower()
    return any(
        marker in normalized
        for marker in (
            "compara",
            "comparar",
            "comparacion",
            "comparación",
            "todas las tasas",
            "las tres tasas",
            "bcv y binance",
            "dolar y euro",
            "dólar y euro",
        )
    )


def build_exchange_rate_prompt_block(settings: dict[str, Any] | None, legacy_manual_rate: Any = None) -> str:
    rate_settings = dict(settings or {})
    if legacy_manual_rate is not None and MANUAL_EXCHANGE_RATE_KEY not in rate_settings:
        rate_settings[MANUAL_EXCHANGE_RATE_KEY] = legacy_manual_rate
    selected = selected_exchange_rate(rate_settings)
    reference: ExchangeRateReference = selected["reference"]
    rate: Decimal | None = selected["rate"]

    if rate is None:
        return (
            f"La referencia de tasa configurada es {reference.label}, pero no hay un valor disponible en este momento. "
            "Si el cliente pregunta por la tasa, explica que está temporalmente no disponible y NO inventes un valor."
        )

    rate_text = format_rate_for_customer(rate)
    if selected["reference_key"] == "manual":
        selected_text = f"tasa manual: {rate_text} Bs por USD"
    else:
        selected_text = f"{reference.label}: {rate_text} Bs por {reference.unit}"
    return (
        f"La tienda usa como referencia {selected_text}. "
        "Si el cliente pregunta por la tasa, responde solo con esta referencia seleccionada. "
        "No muestres las demás tasas salvo que el cliente pida compararlas explícitamente. "
        "No llames Binance a las tasas BCV."
    )
