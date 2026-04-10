"""
Helpers for dynamic store-defined payment methods.
"""

from __future__ import annotations

import re
import unicodedata
from uuid import uuid4

PAYMENT_METHODS_SETTING_KEY = "payment_methods"

LEGACY_PAYMENT_METHOD_SETTINGS = (
    ("payment_zelle_details", "Zelle"),
    ("payment_binance_details", "Binance Pay"),
    ("payment_zinli_details", "Zinli"),
    ("payment_bolivares_details", "Bolívares"),
)

LEGACY_PAYMENT_SETTING_KEYS = tuple(key for key, _ in LEGACY_PAYMENT_METHOD_SETTINGS)


def normalize_payment_methods(payment_methods) -> list[dict]:
    """
    Validate and normalize a payment-method list.

    Each item is stored as:
        {"id": "<uuid>", "name": "<display name>", "information": "<details>"}
    """
    if payment_methods is None:
        return []

    if not isinstance(payment_methods, list):
        raise ValueError("payment_methods must be a list.")

    normalized: list[dict] = []
    seen_names: set[str] = set()

    for item in payment_methods:
        if not isinstance(item, dict):
            raise ValueError("Each payment method must be an object.")

        name = str(item.get("name", "")).strip()
        information = str(item.get("information", "")).strip()

        if not name:
            raise ValueError("Each payment method must have a name.")
        if not information:
            raise ValueError(f"Payment method '{name}' must have information.")

        normalized_name = _normalize_name_key(name)
        if normalized_name in seen_names:
            raise ValueError("Payment method names must be unique.")
        seen_names.add(normalized_name)

        normalized.append({
            "id": str(item.get("id") or uuid4()),
            "name": name,
            "information": information,
        })

    return normalized


def build_payment_methods_from_legacy(settings: dict) -> list[dict]:
    """
    Convert legacy fixed payment settings into the dynamic payment_methods list.
    """
    migrated = []
    for key, display_name in LEGACY_PAYMENT_METHOD_SETTINGS:
        details = str(settings.get(key, "") or "").strip()
        if details:
            migrated.append({
                "id": str(uuid4()),
                "name": display_name,
                "information": details,
            })
    return migrated


def payment_method_names(payment_methods: list[dict] | None) -> list[str]:
    methods = normalize_payment_methods(payment_methods or [])
    return [method["name"] for method in methods]


def payment_method_names_text(payment_methods: list[dict] | None) -> str:
    names = payment_method_names(payment_methods)
    if not names:
        return "ninguno configurado"
    return ", ".join(names)


def payment_method_information_block(payment_methods: list[dict] | None) -> str:
    methods = normalize_payment_methods(payment_methods or [])
    if not methods:
        return (
            "No hay métodos de pago configurados en este momento. "
            "Si el cliente está listo para pagar, explica que la tienda confirmará "
            "los datos de pago manualmente y NO inventes cuentas ni instrucciones."
        )

    return "\n".join(
        f"- **{method['name']}**: {method['information']}"
        for method in methods
    )


def payment_method_tag_value(method_name: str) -> str:
    slug = _normalize_name_key(method_name).replace(" ", "_")
    slug = re.sub(r"[^a-z0-9_]+", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "unknown"


def _normalize_name_key(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name.lower())
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip()
