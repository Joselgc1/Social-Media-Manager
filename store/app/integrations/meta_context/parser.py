"""Parse context-only Meta Instagram comment and Story-reply webhooks."""

import hashlib
import json
from datetime import UTC, datetime

from app.integrations.meta_context.models import MetaInstagramContextEvent


def parse_instagram_context_events(payload: object) -> list[MetaInstagramContextEvent]:
    if not isinstance(payload, dict) or payload.get("object") != "instagram":
        return []

    events: list[MetaInstagramContextEvent] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        account_id = _text(entry.get("id"))
        entry_timestamp = _timestamp(entry.get("time"))
        for messaging_event in entry.get("messaging") or []:
            event = _parse_story_reply(
                messaging_event,
                account_id=account_id,
                entry_timestamp=entry_timestamp,
            )
            if event:
                events.append(event)
        candidates: list[tuple[str | None, object]] = []
        if "value" in entry:
            candidates.append((_text(entry.get("field")), entry.get("value")))
        for change in entry.get("changes") or []:
            if isinstance(change, dict):
                candidates.append((_text(change.get("field")), change.get("value")))

        for field, raw_value in candidates:
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            for value in values:
                event = _parse_comment_value(
                    value,
                    field=field,
                    account_id=account_id,
                    entry_timestamp=entry_timestamp,
                )
                if event:
                    events.append(event)
    return events


def parse_instagram_comment_events(payload: object) -> list[MetaInstagramContextEvent]:
    """Compatibility alias for callers that previously parsed comments only."""
    return parse_instagram_context_events(payload)


def _parse_story_reply(
    value: object,
    *,
    account_id: str | None,
    entry_timestamp: datetime | None,
) -> MetaInstagramContextEvent | None:
    if not isinstance(value, dict):
        return None
    message = value.get("message")
    if not isinstance(message, dict):
        return None
    sender = value.get("sender") if isinstance(value.get("sender"), dict) else {}
    sender_id = _text(sender.get("id"))
    if (
        message.get("is_echo") is True
        or message.get("is_self") is True
        or message.get("is_deleted") is True
        or message.get("deleted") is True
        or value.get("is_echo") is True
        or value.get("is_self") is True
        or sender_id == account_id
    ):
        return None
    reply_to = message.get("reply_to") if isinstance(message.get("reply_to"), dict) else {}
    story = reply_to.get("story") if isinstance(reply_to.get("story"), dict) else {}
    story_id = _text(story.get("id"))
    message_id = _text(message.get("mid") or message.get("message_id"))
    if not story_id or not message_id or not sender_id:
        return None
    return MetaInstagramContextEvent(
        external_event_id=message_id,
        event_type="story_reply",
        instagram_account_id=account_id,
        sender_id=sender_id,
        sender_username=_text(sender.get("username")),
        message_text=_text(message.get("text")) or "",
        event_timestamp=(
            _timestamp(value.get("timestamp"))
            or _timestamp(message.get("timestamp"))
            or entry_timestamp
            or datetime.now(UTC)
        ),
        message_id=message_id,
        media_id=story_id,
        story_id=story_id,
        story_url=_text(story.get("url")),
    )


def _parse_comment_value(
    value: object,
    *,
    field: str | None,
    account_id: str | None,
    entry_timestamp: datetime | None,
) -> MetaInstagramContextEvent | None:
    if not isinstance(value, dict):
        return None
    normalized_field = (field or "").casefold()
    if normalized_field and normalized_field not in {"comments", "comment"}:
        return None

    media = value.get("media") if isinstance(value.get("media"), dict) else {}
    sender = value.get("from") if isinstance(value.get("from"), dict) else {}
    if not sender and isinstance(value.get("sender"), dict):
        sender = value["sender"]
    parent = value.get("parent") if isinstance(value.get("parent"), dict) else {}
    comment_id = _text(value.get("comment_id") or value.get("id"))
    message_id = _text(value.get("message_id") or value.get("mid"))
    message_text = _text(value.get("text") or value.get("message")) or ""
    media_id = _text(value.get("media_id") or media.get("id"))

    if not comment_id and not message_id:
        return None
    if not normalized_field and not (message_text or media_id):
        return None

    timestamp = (
        _timestamp(value.get("created_time"))
        or _timestamp(value.get("timestamp"))
        or entry_timestamp
        or datetime.now(UTC)
    )
    external_event_id = _text(value.get("event_id")) or comment_id or message_id
    if not external_event_id:
        canonical = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        external_event_id = "meta-comment-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return MetaInstagramContextEvent(
        external_event_id=external_event_id,
        instagram_account_id=_text(value.get("instagram_account_id")) or account_id,
        sender_id=_text(sender.get("id") or value.get("sender_id")),
        sender_username=_text(sender.get("username") or value.get("sender_username")),
        message_text=message_text,
        event_timestamp=timestamp,
        comment_id=comment_id,
        parent_comment_id=_text(
            value.get("parent_comment_id") or value.get("parent_id") or parent.get("id")
        ),
        message_id=message_id,
        media_id=media_id,
        media_product_type=_text(
            value.get("media_product_type") or media.get("media_product_type")
        ),
        story_id=_text(value.get("story_id")),
        story_url=_text(value.get("story_url")),
    )


def _timestamp(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None
