"""Normalize Kommo form-encoded webhook payloads."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.integrations.kommo.models import NormalizedKommoEvent

logger = logging.getLogger(__name__)


def parse_nested_form(flat_items: dict[str, Any]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for key, value in flat_items.items():
        parts = _split_form_key(str(key))
        if not parts:
            continue
        _assign_nested(root, parts, value)
    return _lists_from_numeric_dicts(root)


def normalize_kommo_webhook(payload: dict[str, Any]) -> list[NormalizedKommoEvent]:
    data = parse_nested_form(payload) if any("[" in str(key) for key in payload) else payload
    events: list[NormalizedKommoEvent] = []

    for item in _as_list(data.get("add")):
        events.append(_message_event(item, "incoming_message"))

    for item in _message_items(data.get("message")):
        event_type = "outgoing_message" if _is_outgoing_message(item) else "incoming_message"
        events.append(_message_event(item, event_type))

    for item in _as_list((data.get("outgoing_message") or {}).get("add")):
        events.append(_message_event(item, "outgoing_message"))

    for item in _as_list((data.get("leads") or {}).get("update")):
        events.append(_lead_update_event(item))
    for item in _as_list((data.get("leads") or {}).get("status")):
        events.append(_lead_update_event(item))

    for item in _as_list((data.get("contacts") or {}).get("update")):
        if (item or {}).get("type", "contact") == "contact":
            events.append(_contact_update_event(item))

    for item in _as_list((data.get("talk") or {}).get("add")):
        events.append(_talk_event(item, "talk_added"))
    for item in _as_list((data.get("talk") or {}).get("update")):
        events.append(_talk_event(item, "talk_updated"))

    if not events:
        logger.info("Kommo webhook ignored unsupported shape with top-level keys: %s", sorted(map(str, data.keys()))[:8])
    return [event for event in events if event is not None]


def origin_to_channel(origin: str | None) -> str | None:
    normalized = (origin or "").lower()
    if "whatsapp" in normalized or normalized in {"wa", "waba"}:
        return "whatsapp"
    if "instagram" in normalized or normalized in {"ig", "inst"}:
        return "instagram"
    return None


def _message_event(item: dict[str, Any], event_type: str) -> NormalizedKommoEvent:
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    attachment = item.get("attachment") if isinstance(item.get("attachment"), dict) else {}
    origin = _string_or_none(item.get("origin"))
    media_url = None
    if attachment.get("type") in {"picture", "image"}:
        media_url = _string_or_none(attachment.get("link"))
    return NormalizedKommoEvent(
        event_type=event_type,
        message_id=_string_or_none(item.get("id")),
        chat_id=_string_or_none(item.get("chat_id")),
        talk_id=_string_or_none(item.get("talk_id")),
        contact_id=_string_or_none(item.get("contact_id")),
        lead_id=_lead_id(item),
        entity_id=_string_or_none(item.get("entity_id") or item.get("element_id")),
        entity_type=_string_or_none(item.get("entity_type") or item.get("element_type")),
        text=_string_or_none(item.get("text")),
        message_type=_string_or_none(item.get("message_type") or attachment.get("type")),
        origin=origin,
        channel=origin_to_channel(origin),
        author_id=_string_or_none(author.get("id") or author.get("user_id")),
        author_name=_string_or_none(author.get("name")),
        author_type=_string_or_none(author.get("type")),
        created_at=_timestamp(item.get("created_at")),
        media_url=media_url,
    )


def _message_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    items = []
    for key in ("add", "update"):
        items.extend(item for item in _as_list(value.get(key)) if isinstance(item, dict))
    if items:
        return items
    message_keys = {"id", "chat_id", "talk_id", "contact_id", "lead_id", "entity_id", "element_id", "text"}
    return [value] if message_keys & set(value.keys()) else []


def _lead_id(item: dict[str, Any]) -> str | None:
    direct_lead_id = _string_or_none(item.get("lead_id"))
    if direct_lead_id:
        return direct_lead_id
    if (item.get("entity_type") or item.get("element_type")) in {"lead", "2", 2}:
        return _string_or_none(item.get("entity_id") or item.get("element_id"))
    return None


def _is_outgoing_message(item: dict[str, Any]) -> bool:
    direction = str(item.get("type") or item.get("direction") or "").strip().lower()
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    author_type = str(author.get("type") or "").strip().lower()
    return direction == "outgoing" or author_type == "internal"


def _lead_update_event(item: dict[str, Any]) -> NormalizedKommoEvent:
    return NormalizedKommoEvent(
        event_type="lead_updated",
        lead_id=_string_or_none(item.get("id")),
        entity_id=_string_or_none(item.get("id")),
        entity_type="lead",
        author_id=_string_or_none(item.get("modified_user_id")),
        created_at=_timestamp(item.get("updated_at") or item.get("last_modified")),
        ai_mode_enum_id=_extract_ai_mode_enum(item),
    )


def _contact_update_event(item: dict[str, Any]) -> NormalizedKommoEvent:
    return NormalizedKommoEvent(
        event_type="contact_updated",
        contact_id=_string_or_none(item.get("id")),
        entity_id=_string_or_none(item.get("id")),
        entity_type="contact",
        author_id=_string_or_none(item.get("modified_user_id")),
        created_at=_timestamp(item.get("updated_at") or item.get("last_modified")),
    )


def _talk_event(item: dict[str, Any], event_type: str) -> NormalizedKommoEvent:
    origin = _string_or_none(item.get("origin"))
    return NormalizedKommoEvent(
        event_type=event_type,
        chat_id=_string_or_none(item.get("chat_id")),
        talk_id=_string_or_none(item.get("talk_id")),
        contact_id=_string_or_none(item.get("contact_id")),
        lead_id=_string_or_none(item.get("entity_id")) if item.get("entity_type") == "lead" else None,
        entity_id=_string_or_none(item.get("entity_id")),
        entity_type=_string_or_none(item.get("entity_type")),
        origin=origin,
        channel=origin_to_channel(origin),
        created_at=_timestamp(item.get("created_at")),
    )


def _extract_ai_mode_enum(item: dict[str, Any]) -> int | None:
    try:
        from app.config import get_config

        field_id = int(get_config().kommo_ai_mode_field_id or 0)
    except Exception:
        field_id = 0
    for field in _as_list(item.get("custom_fields")) + _as_list(item.get("custom_fields_values")):
        try:
            current_id = int(field.get("id") or field.get("field_id") or 0)
        except (TypeError, ValueError):
            current_id = 0
        if field_id and current_id != field_id:
            continue
        values = _as_list(field.get("values"))
        if not values:
            return None
        first = values[0]
        raw_enum = first.get("enum_id") or first.get("enum") or first.get("value") if isinstance(first, dict) else first
        try:
            return int(raw_enum)
        except (TypeError, ValueError):
            return None
    return None


def _split_form_key(key: str) -> list[str]:
    parts: list[str] = []
    current = ""
    for char in key:
        if char == "[":
            if current:
                parts.append(current)
                current = ""
        elif char == "]":
            parts.append(current)
            current = ""
        else:
            current += char
    if current:
        parts.append(current)
    return [part for part in parts if part != ""]


def _assign_nested(root: dict[str, Any], parts: list[str], value: Any) -> None:
    cursor = root
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value


def _lists_from_numeric_dicts(value: Any) -> Any:
    if isinstance(value, dict):
        converted = {key: _lists_from_numeric_dicts(val) for key, val in value.items()}
        if converted and all(str(key).isdigit() for key in converted):
            return [converted[key] for key in sorted(converted, key=lambda item: int(item))]
        return converted
    if isinstance(value, list):
        return [_lists_from_numeric_dicts(item) for item in value]
    return value


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values()) if all(str(key).isdigit() for key in value) else [value]
    return []


def _string_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _timestamp(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OSError):
        return None
