from app.channels.text_formatting import format_customer_text
from app.integrations.kommo.text_sanitizer import (
    normalize_emoji_for_kommo,
    prepare_kommo_customer_message,
    strip_emoji_characters,
)


def test_whatsapp_formatter_converts_markdown_bold_to_whatsapp_bold():
    assert format_customer_text(" **Oferta especial** ", "whatsapp") == "*Oferta especial*"


def test_whatsapp_formatter_preserves_line_breaks_and_lists():
    text = "**Opciones:**\n- Pijama rosada\n- Set negro"
    assert format_customer_text(text, "whatsapp") == "*Opciones:*\n- Pijama rosada\n- Set negro"


def test_instagram_formatter_removes_bold_markers_and_converts_lists():
    text = "**Opciones:**\n- Pijama rosada\n* Set negro\n1. Ver https://store.example/p*"
    formatted = format_customer_text(text, "instagram")
    assert formatted == "Opciones:\n• Pijama rosada\n• Set negro\n• Ver https://store.example/p*"
    assert "**" not in formatted
    assert "\n* Set" not in formatted


def test_kommo_formatter_uses_safe_emoji_mode_by_default():
    message, diagnostics = prepare_kommo_customer_message(
        "¡Hola! **Oferta** 💕\n¿Qué buscas?",
        "whatsapp",
        {},
    )

    assert message == "¡Hola! *Oferta* ♡\n¿Qué buscas?"
    assert diagnostics["channel"] == "whatsapp"
    assert diagnostics["message_length"] == len(message)
    assert diagnostics["newline_count"] == 1
    assert diagnostics["non_ascii_present"] is True
    assert diagnostics["emoji_present"] is False
    assert diagnostics["replacement_char_present"] is False
    assert diagnostics["literal_question_mark_present"] is True
    assert diagnostics["kommo_emoji_mode"] == "safe"
    assert diagnostics["kommo_strip_emoji_applied"] is False


def test_kommo_formatter_can_preserve_emoji_per_channel():
    message, diagnostics = prepare_kommo_customer_message(
        "¡Hola! **Oferta** 💕",
        "instagram",
        {"kommo_emoji_mode_instagram": "preserve"},
    )

    assert message == "¡Hola! Oferta 💕"
    assert diagnostics["emoji_present"] is True
    assert diagnostics["kommo_emoji_mode"] == "preserve"


def test_kommo_formatter_strips_emoji_without_replacement_characters():
    message, diagnostics = prepare_kommo_customer_message(
        "¡Hola **bella** 💕",
        "whatsapp",
        {"kommo_emoji_mode_whatsapp": "strip"},
    )

    assert message == "¡Hola *bella*"
    assert "?" not in message
    assert "\ufffd" not in message
    assert diagnostics["emoji_present"] is False
    assert diagnostics["kommo_emoji_mode"] == "strip"
    assert diagnostics["kommo_strip_emoji_applied"] is True


def test_legacy_kommo_strip_emoji_overrides_per_channel_mode():
    message, diagnostics = prepare_kommo_customer_message(
        "¡Hola 💕",
        "whatsapp",
        {"kommo_strip_emoji": True, "kommo_emoji_mode_whatsapp": "preserve"},
    )

    assert message == "¡Hola"
    assert diagnostics["kommo_emoji_mode"] == "strip"


def test_safe_emoji_normalization_preserves_accents_punctuation_and_urls():
    message = normalize_emoji_for_kommo("¡Aquí está! 💕 https://store.example/promo?x=1&emoji=💕 😊")
    assert message == "¡Aquí está! ♡ https://store.example/promo?x=1&emoji=💕 :)"
    assert "?" in message
    assert "\ufffd" not in message


def test_emoji_stripping_removes_joined_sequences_cleanly():
    assert strip_emoji_characters("Promo 👩‍💻 lista") == "Promo lista"
