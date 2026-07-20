"""Kommo customer profile normalization and safe local enrichment."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app import db
from app.customer_identity import is_uuid_like, normalize_phone_number

_LEAD_PLACEHOLDER_RE = re.compile(r"^lead\s+#\d+$", re.IGNORECASE)
_HEX_ID_RE = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)
_INSTAGRAM_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_GENERIC_DISPLAY_NAMES = {
    "cliente",
    "cliente whatsapp",
    "cliente instagram",
    "contact",
    "contacto",
    "customer",
    "instagram user",
    "lead",
    "no name",
    "sin nombre",
    "unknown",
    "user",
    "usuario",
    "usuario instagram",
    "whatsapp user",
}
_GENERIC_HANDLES = {
    "cliente",
    "contacto",
    "customer",
    "instagram",
    "instagramuser",
    "profile",
    "perfil",
    "unknown",
    "user",
    "usuario",
}
_RESERVED_INSTAGRAM_PATHS = {"explore", "p", "reel", "reels", "stories", "tv"}
_MOBILE_MARKERS = {"cell", "cel", "celular", "mobile", "movil", "móvil", "mob"}
_INSTAGRAM_MARKERS = {"ig", "inst", "insta", "instagram"}
_MESSAGING_MARKERS = {"im", "messaging", "messenger", "social", "red social", "redes sociales"}


@dataclass(frozen=True)
class KommoCustomerProfile:
    display_name: str | None = None
    phone: str | None = None
    instagram_handle: str | None = None

    def as_customer_profile(self) -> dict[str, str | None]:
        return {
            "display_name": self.display_name,
            "phone": self.phone,
            "instagram_handle": self.instagram_handle,
        }


def build_kommo_customer_profile(
    *,
    job: dict[str, Any],
    contact: dict[str, Any] | None = None,
) -> KommoCustomerProfile:
    """Build customer-facing profile data from safe Kommo sources only."""
    display_name = first_meaningful_display_name(
        _value(job, "author_name"),
        _contact_full_name(contact),
        _value(contact or {}, "name"),
    )
    return KommoCustomerProfile(
        display_name=display_name,
        phone=extract_contact_phone(contact),
        instagram_handle=extract_instagram_handle(contact),
    )


async def enrich_customer_profile(
    customer: dict[str, Any],
    profile: KommoCustomerProfile,
    *,
    identifiers: set[str] | None = None,
) -> dict[str, Any]:
    """Update one existing customer with safe profile fields and last_active."""
    updates = compute_customer_profile_updates(customer, profile, identifiers=identifiers)
    values = {"id": str(customer["id"])} | updates
    set_clauses = ["last_active = NOW()"]
    set_clauses.extend(f"{field} = :{field}" for field in updates)

    await db.execute(
        f"UPDATE customers SET {', '.join(set_clauses)} WHERE id::text = :id",
        values,
    )
    refreshed = await db.fetch_one("SELECT * FROM customers WHERE id::text = :id", {"id": str(customer["id"])})
    return dict(refreshed) if refreshed else customer | updates


def compute_customer_profile_updates(
    customer: dict[str, Any],
    profile: KommoCustomerProfile,
    *,
    identifiers: set[str] | None = None,
) -> dict[str, str | None]:
    identifiers = {str(value).strip() for value in identifiers or set() if str(value or "").strip()}
    updates: dict[str, str | None] = {}

    current_display = _text(_value(customer, "display_name"))
    incoming_display = normalize_display_name(profile.display_name)
    if incoming_display and not normalize_display_name(current_display):
        updates["display_name"] = incoming_display
    elif current_display and not normalize_display_name(current_display):
        updates["display_name"] = None

    current_phone = _text(_value(customer, "phone"))
    incoming_phone = normalize_phone_number(profile.phone)
    if incoming_phone and not normalize_phone_number(current_phone):
        updates["phone"] = incoming_phone
    elif current_phone and _should_clear_invalid_identifier_value(current_phone, identifiers, kind="phone"):
        updates["phone"] = None

    current_handle = _text(_value(customer, "instagram_handle"))
    incoming_handle = normalize_instagram_handle(profile.instagram_handle)
    normalized_current_handle = normalize_instagram_handle(current_handle)
    if incoming_handle and not normalized_current_handle:
        updates["instagram_handle"] = incoming_handle
    elif normalized_current_handle and current_handle != normalized_current_handle:
        updates["instagram_handle"] = normalized_current_handle
    elif current_handle and _should_clear_invalid_identifier_value(current_handle, identifiers, kind="instagram"):
        updates["instagram_handle"] = None

    return updates


def first_meaningful_display_name(*values: str | None) -> str | None:
    for value in values:
        normalized = normalize_display_name(value)
        if normalized:
            return normalized
    return None


def normalize_display_name(value: str | None) -> str | None:
    text = _collapse_spaces(value)
    if not text:
        return None
    if len(text) > 80:
        return None
    if _LEAD_PLACEHOLDER_RE.fullmatch(text):
        return None
    if is_uuid_like(text) or text.isdigit() or _HEX_ID_RE.fullmatch(text):
        return None
    lowered = _normalize_label(text)
    if lowered in _GENERIC_DISPLAY_NAMES:
        return None
    if "://" in text or text.lower().startswith("www."):
        return None
    return text


def extract_contact_phone(contact: dict[str, Any] | None) -> str | None:
    candidates: list[tuple[bool, str]] = []
    for field in _custom_fields(contact):
        if _field_code(field) != "PHONE":
            continue
        for value in _field_values(field):
            raw = _text(_value(value, "value") if isinstance(value, dict) else value)
            phone = normalize_phone_number(raw)
            if not phone:
                continue
            candidates.append((_is_mobile_value(value), phone))

    for is_mobile, phone in candidates:
        if is_mobile:
            return phone
    return candidates[0][1] if candidates else None


def extract_instagram_handle(contact: dict[str, Any] | None) -> str | None:
    for field in _custom_fields(contact):
        field_label = _field_label(field)
        field_is_instagram = _has_marker(field_label, _INSTAGRAM_MARKERS)
        field_is_messaging = _has_marker(field_label, _MESSAGING_MARKERS)
        for value in _field_values(field):
            raw = _text(_value(value, "value") if isinstance(value, dict) else value)
            value_label = _field_label(value) if isinstance(value, dict) else ""
            explicit_value = _looks_like_explicit_instagram_value(raw)
            explicit_im = field_is_messaging and _has_marker(value_label, _INSTAGRAM_MARKERS)
            if not (field_is_instagram or explicit_im or explicit_value):
                continue
            handle = normalize_instagram_handle(raw)
            if handle:
                return handle
    return None


def normalize_instagram_handle(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text or is_uuid_like(text):
        return None

    if "instagram.com" in text.lower():
        parsed = urlparse(text if "://" in text else f"https://{text}")
        host = parsed.netloc.lower()
        if host not in {"instagram.com", "www.instagram.com", "m.instagram.com"}:
            return None
        path_parts = [part for part in parsed.path.split("/") if part]
        if not path_parts:
            return None
        if path_parts[0].lower() in _RESERVED_INSTAGRAM_PATHS:
            return None
        text = path_parts[0]

    text = text.split("?", 1)[0].split("#", 1)[0].strip().strip("/").lstrip("@")
    if not text or text.isdigit() or " " in text or _normalize_label(text) in _GENERIC_HANDLES:
        return None
    if not _INSTAGRAM_HANDLE_RE.fullmatch(text):
        return None
    if text.startswith(".") or text.endswith(".") or ".." in text:
        return None
    return text


def kommo_identifier_values(*sources: dict[str, Any] | None) -> set[str]:
    keys = {
        "author_id",
        "chat_id",
        "contact_id",
        "external_author_id",
        "external_chat_id",
        "external_contact_id",
        "external_lead_id",
        "external_talk_id",
        "lead_id",
        "talk_id",
    }
    values: set[str] = set()
    for source in sources:
        for key in keys:
            value = _value(source or {}, key)
            if value not in (None, ""):
                values.add(str(value).strip())
    return {value for value in values if value}


def _contact_full_name(contact: dict[str, Any] | None) -> str | None:
    first_name = _text(_value(contact or {}, "first_name"))
    last_name = _text(_value(contact or {}, "last_name"))
    full_name = " ".join(part for part in (first_name, last_name) if part)
    return full_name or None


def _should_clear_invalid_identifier_value(value: str, identifiers: set[str], *, kind: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    if stripped in identifiers or is_uuid_like(stripped):
        return True
    if kind == "phone":
        return normalize_phone_number(stripped) is None and stripped.isdigit() and stripped in identifiers
    if kind == "instagram":
        return normalize_instagram_handle(stripped) is None and (stripped in identifiers or is_uuid_like(stripped))
    return False


def _custom_fields(contact: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [field for field in _as_list(_value(contact or {}, "custom_fields_values")) if isinstance(field, dict)]


def _field_values(field: dict[str, Any]) -> list[Any]:
    return _as_list(_value(field, "values"))


def _field_code(field: dict[str, Any]) -> str:
    return str(_value(field, "field_code") or _value(field, "code") or "").strip().upper()


def _field_label(field: dict[str, Any]) -> str:
    pieces = [
        _value(field, "field_code"),
        _value(field, "field_name"),
        _value(field, "name"),
        _value(field, "enum_code"),
        _value(field, "enum"),
        _value(field, "enum_name"),
        _value(field, "label"),
    ]
    return " ".join(str(piece) for piece in pieces if piece not in (None, ""))


def _is_mobile_value(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return _has_marker(_field_label(value), _MOBILE_MARKERS)


def _looks_like_explicit_instagram_value(value: str | None) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith("@") or "instagram.com" in text


def _has_marker(value: str, markers: set[str]) -> bool:
    normalized = _normalize_label(value)
    tokens = set(re.split(r"[^a-z0-9]+", normalized))
    for marker in markers:
        if len(marker) <= 3:
            if marker in tokens:
                return True
            continue
        if marker in normalized or marker in tokens:
            return True
    return False


def _normalize_label(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip()


def _collapse_spaces(value: str | None) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or None


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values()) if value and all(str(key).isdigit() for key in value) else [value]
    return [value]


def _value(row: Any, key: str, default=None):
    try:
        return row[key]
    except (KeyError, TypeError):
        return default
