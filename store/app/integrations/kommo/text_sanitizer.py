"""Final Kommo-bound message formatting and safe diagnostics."""

from __future__ import annotations

import re

from app.channels.text_formatting import format_customer_text

_SUPPORTED_CHANNELS = {"whatsapp"}
_EMOJI_MODES = {"preserve", "safe", "strip"}
_COMMON_EMOJI_REPLACEMENTS = {
    "❤": "♡",
    "❤️": "♡",
    "💌": "♡",
    "💕": "♡",
    "💗": "♡",
    "💖": "♡",
    "💘": "♡",
    "💜": "♡",
    "✨": "*",
    "⭐": "*",
    "🌟": "*",
    "✅": "OK",
    "☑": "OK",
    "🛍": "[bolsa]",
    "🛍️": "[bolsa]",
    "📦": "[paquete]",
    "🚚": "[envio]",
    "😊": ":)",
    "😉": ";)",
    "😍": ":)",
    "🥰": ":)",
    "😘": ":)",
    "👇": "->",
    "👉": "->",
}
_URL_RE = re.compile(r"https?://\S+")


def prepare_kommo_customer_message(
    text: str | None,
    channel: str | None,
    settings: dict | None,
    *,
    interaction_type: str = "private_message",
) -> tuple[str, dict]:
    destination_channel = channel if channel in _SUPPORTED_CHANNELS else "whatsapp"
    emoji_mode = _emoji_mode_for_channel(destination_channel, settings or {})
    message = format_customer_text(text, destination_channel)
    if emoji_mode == "strip":
        message = strip_emoji_characters(message)
    elif emoji_mode == "safe":
        message = normalize_emoji_for_kommo(message)
    message = message.strip()
    return message, build_kommo_message_diagnostics(message, destination_channel, emoji_mode, interaction_type=interaction_type)


def build_kommo_message_diagnostics(
    message: str,
    channel: str | None,
    emoji_mode: str | bool,
    *,
    interaction_type: str = "private_message",
) -> dict:
    text = message or ""
    mode = _normalize_emoji_mode(emoji_mode)
    return {
        "channel": channel if channel in _SUPPORTED_CHANNELS else "unknown",
        "interaction_type": "private_message",
        "message_length": len(text),
        "newline_count": text.count("\n"),
        "non_ascii_present": any(ord(char) > 127 for char in text),
        "emoji_present": contains_emoji(text),
        "replacement_char_present": "\ufffd" in text,
        "literal_question_mark_present": "?" in text,
        "kommo_emoji_mode": mode,
        "kommo_strip_emoji_applied": mode == "strip",
    }


def normalize_emoji_for_kommo(text: str) -> str:
    return _normalize_text_outside_urls(text, _safe_emoji_segment)


def strip_emoji_characters(text: str) -> str:
    return _normalize_text_outside_urls(text, _strip_emoji_segment)


def _safe_emoji_segment(text: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(text):
        replacement = None
        replacement_length = 0
        for emoji, value in _COMMON_EMOJI_REPLACEMENTS.items():
            if text.startswith(emoji, index) and len(emoji) > replacement_length:
                replacement = value
                replacement_length = len(emoji)
        if replacement is not None:
            output.append(replacement)
            index += replacement_length
            continue

        char = text[index]
        if _is_emoji_codepoint(ord(char)):
            index += 1
            while index < len(text) and _is_emoji_modifier_codepoint(ord(text[index])):
                index += 1
            continue
        output.append(char)
        index += 1

    return _cleanup_emoji_spacing("".join(output))


def _strip_emoji_segment(text: str) -> str:
    stripped = "".join(char for char in text if not _is_emoji_codepoint(ord(char)))
    return _cleanup_emoji_spacing(stripped)


def _cleanup_emoji_spacing(text: str) -> str:
    stripped = re.sub(r"[ \t]{2,}", " ", text)
    stripped = re.sub(r"[ \t]+\n", "\n", stripped)
    stripped = re.sub(r"\n[ \t]+", "\n", stripped)
    return stripped


def contains_emoji(text: str) -> bool:
    return any(_is_emoji_codepoint(ord(char)) for char in text or "")


def _setting_enabled(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _emoji_mode_for_channel(channel: str, settings: dict) -> str:
    if _setting_enabled(settings.get("kommo_strip_emoji", False)):
        return "strip"
    mode = _normalize_emoji_mode(settings.get(f"kommo_emoji_mode_{channel}"))
    if mode:
        return mode
    return "safe"


def _normalize_emoji_mode(value) -> str:
    if isinstance(value, bool):
        return "strip" if value else "preserve"
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _EMOJI_MODES:
            return normalized
    return ""


def _normalize_text_outside_urls(text: str, transform) -> str:
    parts: list[str] = []
    last_end = 0
    for match in _URL_RE.finditer(text):
        parts.append(transform(text[last_end:match.start()]))
        parts.append(match.group(0))
        last_end = match.end()
    parts.append(transform(text[last_end:]))
    return "".join(parts).strip()


def _is_emoji_codepoint(codepoint: int) -> bool:
    if codepoint in {0x2661}:
        return False
    return (
        codepoint in {0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299}
        or 0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or 0xFE00 <= codepoint <= 0xFE0F
        or 0xE0020 <= codepoint <= 0xE007F
        or codepoint == 0x200D
    )


def _is_emoji_modifier_codepoint(codepoint: int) -> bool:
    return codepoint == 0x200D or 0xFE00 <= codepoint <= 0xFE0F or 0x1F3FB <= codepoint <= 0x1F3FF
