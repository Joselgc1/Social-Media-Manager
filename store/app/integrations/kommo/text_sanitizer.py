"""Final Kommo-bound message formatting and safe diagnostics."""

from __future__ import annotations

import re

from app.channels.text_formatting import format_customer_text

_SUPPORTED_CHANNELS = {"whatsapp", "instagram"}


def prepare_kommo_customer_message(text: str | None, channel: str | None, settings: dict | None) -> tuple[str, dict]:
    destination_channel = channel if channel in _SUPPORTED_CHANNELS else "whatsapp"
    strip_emoji = _setting_enabled((settings or {}).get("kommo_strip_emoji", False))
    message = format_customer_text(text, destination_channel)
    if strip_emoji:
        message = strip_emoji_characters(message)
    message = message.strip()
    return message, build_kommo_message_diagnostics(message, destination_channel, strip_emoji)


def build_kommo_message_diagnostics(message: str, channel: str | None, strip_emoji_applied: bool) -> dict:
    text = message or ""
    return {
        "channel": channel if channel in _SUPPORTED_CHANNELS else "unknown",
        "message_length": len(text),
        "newline_count": text.count("\n"),
        "non_ascii_present": any(ord(char) > 127 for char in text),
        "emoji_present": contains_emoji(text),
        "replacement_char_present": "\ufffd" in text,
        "literal_question_mark_present": "?" in text,
        "kommo_strip_emoji_applied": bool(strip_emoji_applied),
    }


def strip_emoji_characters(text: str) -> str:
    stripped = "".join(char for char in text if not _is_emoji_codepoint(ord(char)))
    stripped = re.sub(r"[ \t]{2,}", " ", stripped)
    stripped = re.sub(r"[ \t]+\n", "\n", stripped)
    stripped = re.sub(r"\n[ \t]+", "\n", stripped)
    return stripped.strip()


def contains_emoji(text: str) -> bool:
    return any(_is_emoji_codepoint(ord(char)) for char in text or "")


def _setting_enabled(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _is_emoji_codepoint(codepoint: int) -> bool:
    return (
        codepoint in {0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299}
        or 0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or 0xFE00 <= codepoint <= 0xFE0F
        or 0xE0020 <= codepoint <= 0xE007F
        or codepoint == 0x200D
    )
