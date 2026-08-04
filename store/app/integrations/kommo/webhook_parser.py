"""Normalize Kommo form-encoded webhook payloads."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.integrations.kommo.models import NormalizedKommoEvent

logger = logging.getLogger(__name__)

INCOMING_MEDIA_TYPES = {"picture", "image", "voice", "audio"}


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
    default_interaction_type = normalize_interaction_type(data.get("interaction_type"))

    chat_api_event = _chat_api_message_event(data, default_interaction_type=default_interaction_type)
    if chat_api_event:
        events.append(chat_api_event)

    chat_api_payload_event = _chat_api_payload_event(data, default_interaction_type=default_interaction_type)
    if chat_api_payload_event:
        events.append(chat_api_payload_event)

    for item in _as_list(data.get("add")):
        events.append(_message_event(item, "incoming_message", default_interaction_type=default_interaction_type))

    for item in _message_items(data.get("message")):
        event_type = "outgoing_message" if _is_outgoing_message(item) else "incoming_message"
        events.append(_message_event(item, event_type, default_interaction_type=default_interaction_type))

    for item in _as_list((data.get("outgoing_message") or {}).get("add")):
        events.append(_message_event(item, "outgoing_message", default_interaction_type=default_interaction_type))

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


def normalize_interaction_type(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in {"private_message", "instagram_comment"} else None


def _message_event(
    item: dict[str, Any],
    event_type: str,
    *,
    default_interaction_type: str | None = None,
) -> NormalizedKommoEvent:
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
    attachment = item.get("attachment") if isinstance(item.get("attachment"), dict) else {}
    origin = _string_or_none(item.get("origin"))
    attachment_type = _string_or_none(attachment.get("type"))
    message_type = _string_or_none(item.get("message_type") or attachment_type)
    if str(attachment_type or "").strip().lower() in {"voice", "audio"}:
        message_type = attachment_type
    channel = origin_to_channel(origin)
    media_url = None
    if str(attachment_type or "").strip().lower() in INCOMING_MEDIA_TYPES:
        media_url = _string_or_none(attachment.get("link"))
    explicit_interaction_type = normalize_interaction_type(item.get("interaction_type")) or default_interaction_type
    comment_fields = _comment_fields(item)
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
        message_type=message_type,
        origin=origin,
        channel=channel,
        interaction_type=explicit_interaction_type or _infer_interaction_type(channel, message_type),
        author_id=_string_or_none(author.get("id") or author.get("user_id")),
        author_name=_string_or_none(author.get("name")),
        author_type=_string_or_none(author.get("type")),
        **_profile_identity_fields(author=author, sender=sender, item=item),
        created_at=_timestamp(item.get("created_at")),
        media_url=media_url,
        **comment_fields,
    )


def _chat_api_message_event(
    data: dict[str, Any],
    *,
    default_interaction_type: str | None = None,
) -> NormalizedKommoEvent | None:
    """Normalize documented Chats API webhooks sent from Kommo to a custom channel."""
    wrapper = data.get("message") if isinstance(data.get("message"), dict) else {}
    message = wrapper.get("message") if isinstance(wrapper.get("message"), dict) else {}
    conversation = wrapper.get("conversation") if isinstance(wrapper.get("conversation"), dict) else {}
    if not message or not conversation:
        return None

    sender = wrapper.get("sender") if isinstance(wrapper.get("sender"), dict) else {}
    source = wrapper.get("source") if isinstance(wrapper.get("source"), dict) else {}
    origin = _first_string(wrapper.get("origin"), source.get("external_id"))
    return NormalizedKommoEvent(
        event_type="outgoing_message",
        message_id=_first_string(message.get("id"), message.get("client_id"), message.get("msgid")),
        chat_id=_first_string(conversation.get("id"), conversation.get("client_id")),
        text=_string_or_none(message.get("text")),
        message_type=_string_or_none(message.get("type")),
        origin=origin,
        channel=origin_to_channel(origin),
        interaction_type=default_interaction_type or _infer_interaction_type(origin_to_channel(origin), message.get("type")),
        author_id=_first_string(sender.get("id"), sender.get("ref_id")),
        author_name=_string_or_none(sender.get("name")),
        author_type="internal",
        **_profile_identity_fields(author={}, sender=sender, item=wrapper),
        created_at=_timestamp(wrapper.get("timestamp") or data.get("time")),
        media_url=_string_or_none(message.get("media")),
        **_chat_api_comment_fields(message),
    )


def _chat_api_payload_event(
    data: dict[str, Any],
    *,
    default_interaction_type: str | None = None,
) -> NormalizedKommoEvent | None:
    """Normalize documented Chats API send/import payloads when posted to this parser."""
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    if not payload or not message or not sender:
        return None

    receiver = payload.get("receiver") if isinstance(payload.get("receiver"), dict) else {}
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    origin = _first_string(payload.get("origin"), source.get("external_id"))
    event_type = "outgoing_message" if receiver else "incoming_message"
    author_type = "internal" if receiver else "external"
    return NormalizedKommoEvent(
        event_type=event_type,
        message_id=_first_string(payload.get("msgid"), message.get("id"), message.get("client_id")),
        chat_id=_first_string(payload.get("conversation_ref_id"), payload.get("conversation_id")),
        text=_string_or_none(message.get("text")),
        message_type=_string_or_none(message.get("type")),
        origin=origin,
        channel=origin_to_channel(origin),
        interaction_type=default_interaction_type or _infer_interaction_type(origin_to_channel(origin), message.get("type")),
        author_id=_first_string(sender.get("id"), sender.get("ref_id")),
        author_name=_string_or_none(sender.get("name")),
        author_type=author_type,
        **_profile_identity_fields(author={}, sender=sender, item=payload),
        created_at=_timestamp(payload.get("timestamp")),
        media_url=_string_or_none(message.get("media")),
        **_chat_api_comment_fields(message),
    )


def _infer_interaction_type(channel: str | None, message_type: str | None) -> str:
    if channel == "instagram" and str(message_type or "").strip().lower() == "comment":
        return "instagram_comment"
    return "private_message"


def _comment_fields(item: dict[str, Any]) -> dict[str, str | None]:
    comment = item.get("comment") if isinstance(item.get("comment"), dict) else {}
    post = item.get("post") if isinstance(item.get("post"), dict) else {}
    media = item.get("media") if isinstance(item.get("media"), dict) else {}
    return {
        "post_id": _first_string(item.get("post_id"), post.get("id"), post.get("post_id")),
        "comment_id": _first_string(item.get("comment_id"), comment.get("id"), comment.get("comment_id")),
        "parent_comment_id": _first_string(
            item.get("parent_comment_id"),
            comment.get("parent_id"),
            comment.get("parent_comment_id"),
        ),
        "media_id": _first_string(item.get("media_id"), media.get("id"), post.get("media_id")),
        "post_url": _first_string(item.get("post_url"), post.get("url"), post.get("link"), media.get("permalink")),
        "comment_url": _first_string(item.get("comment_url"), comment.get("url"), comment.get("link")),
    }


def _chat_api_comment_fields(message: dict[str, Any]) -> dict[str, str | None]:
    post = message.get("post") if isinstance(message.get("post"), dict) else {}
    return {
        "post_id": _first_string(post.get("id")),
        "comment_id": None,
        "parent_comment_id": None,
        "media_id": None,
        "post_url": _first_string(post.get("url")),
        "comment_url": None,
    }


def _profile_identity_fields(
    *,
    author: dict[str, Any],
    sender: dict[str, Any],
    item: dict[str, Any],
) -> dict[str, str | None]:
    return {
        "author_username": _first_string(
            author.get("username"),
            author.get("handle"),
            author.get("login"),
            item.get("author_username"),
            item.get("author_handle"),
            item.get("author_login"),
        ),
        "author_profile_url": _first_string(
            author.get("profile_link"),
            author.get("profile_url"),
            author.get("profile"),
            author.get("url"),
            author.get("link"),
            author.get("permalink"),
            item.get("author_profile_link"),
            item.get("author_profile_url"),
        ),
        "sender_username": _first_string(
            sender.get("username"),
            sender.get("handle"),
            sender.get("login"),
            item.get("sender_username"),
            item.get("sender_handle"),
            item.get("sender_login"),
        ),
        "sender_profile_url": _first_string(
            sender.get("profile_link"),
            sender.get("profile_url"),
            sender.get("profile"),
            sender.get("url"),
            sender.get("link"),
            sender.get("permalink"),
            item.get("sender_profile_link"),
            item.get("sender_profile_url"),
        ),
    }


def _first_string(*values: Any) -> str | None:
    for value in values:
        text = _string_or_none(value)
        if text:
            return text
    return None


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
