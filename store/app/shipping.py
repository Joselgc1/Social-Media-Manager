"""Store-owned delivery policy validation and quote resolution."""

from __future__ import annotations

import re
import unicodedata

DEFAULT_SHIPPING_POLICY = {
    "currency": "USD",
    "home_delivery_cities": [
        {"name": "Valencia", "aliases": ["Valencia, Carabobo"]},
        {"name": "Naguanagua", "aliases": []},
        {"name": "San Diego", "aliases": ["San Diego, Carabobo"]},
    ],
    "home_delivery_zones": [],
    "courier_destination_rates": [],
}
VALID_COURIERS = {"mrw", "zoom"}


def normalize_location(value: object) -> str:
    """Return a comparison key for a configured city, zone, or alias."""
    text = unicodedata.normalize("NFKD", str(value or "").lower())
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text).strip()


def normalize_shipping_policy(raw_policy: object) -> dict:
    """Validate and normalize dashboard-managed delivery pricing rules."""
    if raw_policy is None:
        raw_policy = DEFAULT_SHIPPING_POLICY
    if not isinstance(raw_policy, dict):
        raise ValueError("La política de envío debe ser un objeto.")

    currency = str(raw_policy.get("currency") or "USD").strip().upper()
    if currency != "USD":
        raise ValueError("Actualmente las tarifas de envío deben estar en USD.")

    cities = _normalize_home_cities(raw_policy.get("home_delivery_cities", []))
    city_keys = {
        normalize_location(candidate)
        for city in cities
        for candidate in [city["name"], *(city.get("aliases") or [])]
    }
    zones = _normalize_home_zones(raw_policy.get("home_delivery_zones", []), cities)
    courier_rates = _normalize_courier_rates(raw_policy.get("courier_destination_rates", []), city_keys)
    return {
        "currency": currency,
        "home_delivery_cities": cities,
        "home_delivery_zones": zones,
        "courier_destination_rates": courier_rates,
    }


def resolve_delivery_quote(
    policy: object,
    *,
    city: object,
    delivery_zone: object = None,
    shipping_method: object = None,
) -> dict:
    """Resolve a deterministic delivery quote from configured policy data."""
    normalized_policy = normalize_shipping_policy(policy)
    city_text = _clean_text(city, "Ciudad", required=False)
    if not city_text:
        return {"status": "needs_city", "message": "Falta la ciudad para calcular la entrega."}

    home_city = _match_location(normalized_policy["home_delivery_cities"], city_text)
    if home_city:
        return _resolve_home_delivery(normalized_policy, home_city, delivery_zone)

    courier = str(shipping_method or "").strip().lower()
    if courier not in VALID_COURIERS:
        return {
            "status": "needs_shipping_method",
            "message": "Para entrega en agencia falta escoger MRW o Zoom.",
            "fulfillment_type": "courier_agency_pickup",
        }

    rate = _match_location(normalized_policy["courier_destination_rates"], city_text)
    if not rate:
        return {
            "status": "rate_unavailable",
            "message": "No hay una tarifa configurada para esa ciudad. Debe confirmarse antes de crear el pedido.",
            "fulfillment_type": "courier_agency_pickup",
            "shipping_city": city_text,
            "shipping_method": courier,
        }

    fee = rate[f"{courier}_fee_usd"]
    return {
        "status": "quoted",
        "fulfillment_type": "courier_agency_pickup",
        "shipping_city": rate["city"],
        "shipping_method": courier,
        "shipping_fee": fee,
        "shipping_currency": normalized_policy["currency"],
        "message": f"Tarifa de envío a agencia: ${fee:.2f} USD.",
    }


def _resolve_home_delivery(policy: dict, city: dict, delivery_zone: object) -> dict:
    zone_text = _clean_text(delivery_zone, "Zona", required=False)
    matching_zones = [
        zone for zone in policy["home_delivery_zones"]
        if normalize_location(zone["city"]) == normalize_location(city["name"])
    ]
    if not zone_text:
        return {
            "status": "needs_delivery_zone",
            "message": "Falta la zona para calcular la entrega a domicilio.",
            "fulfillment_type": "home_delivery",
            "shipping_city": city["name"],
            "available_zones": [zone["name"] for zone in matching_zones],
        }

    zone = _match_location(matching_zones, zone_text)
    if not zone:
        return {
            "status": "rate_unavailable",
            "message": "No hay una tarifa configurada para esa zona. Debe confirmarse antes de crear el pedido.",
            "fulfillment_type": "home_delivery",
            "shipping_city": city["name"],
            "available_zones": [configured_zone["name"] for configured_zone in matching_zones],
        }

    fee = zone["fee_usd"]
    return {
        "status": "quoted",
        "fulfillment_type": "home_delivery",
        "shipping_city": city["name"],
        "shipping_zone": zone["name"],
        "shipping_method": None,
        "shipping_fee": fee,
        "shipping_currency": policy["currency"],
        "message": (
            "La entrega a domicilio es gratis para esa zona."
            if fee == 0
            else f"Tarifa de entrega a domicilio: ${fee:.2f} USD."
        ),
    }


def _normalize_home_cities(raw_cities: object) -> list[dict]:
    if not isinstance(raw_cities, list):
        raise ValueError("Las ciudades con entrega a domicilio deben ser una lista.")
    cities = []
    seen = set()
    for raw_city in raw_cities:
        if not isinstance(raw_city, dict):
            raise ValueError("Cada ciudad de entrega a domicilio es inválida.")
        name = _clean_text(raw_city.get("name"), "El nombre de la ciudad")
        aliases = _normalize_aliases(raw_city.get("aliases"), name)
        keys = {normalize_location(candidate) for candidate in [name, *aliases]}
        if seen.intersection(keys):
            raise ValueError("Las ciudades y alias de entrega a domicilio no pueden repetirse.")
        seen.update(keys)
        cities.append({"name": name, "aliases": aliases})
    return cities


def _normalize_home_zones(raw_zones: object, home_cities: list[dict]) -> list[dict]:
    if not isinstance(raw_zones, list):
        raise ValueError("Las zonas de entrega a domicilio deben ser una lista.")
    zones = []
    seen = set()
    for raw_zone in raw_zones:
        if not isinstance(raw_zone, dict):
            raise ValueError("Cada zona de entrega es inválida.")
        city = _clean_text(raw_zone.get("city"), "La ciudad de la zona")
        home_city = _match_location(home_cities, city)
        if not home_city:
            raise ValueError("Cada zona debe pertenecer a una ciudad con entrega a domicilio.")
        city = home_city["name"]
        name = _clean_text(raw_zone.get("name"), "El nombre de la zona")
        aliases = _normalize_aliases(raw_zone.get("aliases"), name)
        keys = {(normalize_location(city), normalize_location(candidate)) for candidate in [name, *aliases]}
        if seen.intersection(keys):
            raise ValueError("Las zonas y alias no pueden repetirse dentro de la misma ciudad.")
        seen.update(keys)
        zones.append({
            "city": city,
            "name": name,
            "aliases": aliases,
            "fee_usd": _clean_fee(raw_zone.get("fee_usd")),
        })
    return zones


def _normalize_courier_rates(raw_rates: object, home_city_keys: set[str]) -> list[dict]:
    if not isinstance(raw_rates, list):
        raise ValueError("Las tarifas de envío a agencia deben ser una lista.")
    rates = []
    seen = set()
    for raw_rate in raw_rates:
        if not isinstance(raw_rate, dict):
            raise ValueError("Cada tarifa por ciudad es inválida.")
        city = _clean_text(raw_rate.get("city"), "La ciudad de destino")
        aliases = _normalize_aliases(raw_rate.get("aliases"), city)
        keys = {normalize_location(candidate) for candidate in [city, *aliases]}
        if seen.intersection(keys):
            raise ValueError("Las ciudades y alias de envío a agencia no pueden repetirse.")
        if home_city_keys.intersection(keys):
            raise ValueError("Una ciudad con entrega a domicilio no puede tener tarifa de retiro en agencia.")
        seen.update(keys)
        rates.append({
            "city": city,
            "aliases": aliases,
            "mrw_fee_usd": _clean_fee(raw_rate.get("mrw_fee_usd")),
            "zoom_fee_usd": _clean_fee(raw_rate.get("zoom_fee_usd")),
        })
    return rates


def _match_location(entries: list[dict], value: str) -> dict | None:
    normalized_value = normalize_location(value)
    for entry in entries:
        values = [entry.get("name") or entry.get("city"), *(entry.get("aliases") or [])]
        if normalized_value in {normalize_location(candidate) for candidate in values}:
            return entry
    return None


def _normalize_aliases(raw_aliases: object, name: str) -> list[str]:
    if raw_aliases in (None, ""):
        return []
    if not isinstance(raw_aliases, list):
        raise ValueError("Los alias deben ser una lista.")
    aliases = []
    seen = {normalize_location(name)}
    for raw_alias in raw_aliases:
        alias = _clean_text(raw_alias, "Cada alias")
        key = normalize_location(alias)
        if key not in seen:
            seen.add(key)
            aliases.append(alias)
    return aliases


def _clean_text(value: object, label: str, *, required: bool = True) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text and required:
        raise ValueError(f"{label} es obligatorio.")
    if len(text) > 100:
        raise ValueError(f"{label} no puede superar 100 caracteres.")
    return text


def _clean_fee(value: object) -> float:
    try:
        fee = round(float(value), 2)
    except (TypeError, ValueError) as exc:
        raise ValueError("Cada tarifa debe ser un monto válido en USD.") from exc
    if not 0 <= fee <= 100000:
        raise ValueError("Cada tarifa debe estar entre 0 y 100000 USD.")
    return fee
