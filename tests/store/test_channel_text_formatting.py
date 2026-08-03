from app.channels.text_formatting import format_customer_text
from app.integrations.kommo.text_sanitizer import (
    normalize_emoji_for_kommo,
    prepare_kommo_customer_message,
    strip_emoji_characters,
)


def test_whatsapp_formatter_converts_markdown_bold_to_whatsapp_bold():
    assert format_customer_text(" **Oferta especial** ", "whatsapp") == "*Oferta especial*"


def test_whatsapp_formatter_converts_common_markup_to_whatsapp_syntax():
    text = (
        "# Promo\n"
        "**Nuevo** __VIP__ _suave_ ~~agotado~~ `SKU-1` "
        "[Ver catálogo](https://store.example/catalogo_v1)"
    )

    assert format_customer_text(text, "whatsapp") == (
        "*Promo*\n"
        "*Nuevo* *VIP* _suave_ ~agotado~ ```SKU-1``` "
        "Ver catálogo: https://store.example/catalogo_v1"
    )


def test_whatsapp_formatter_converts_html_markup_to_whatsapp_syntax():
    text = "<strong>Oferta</strong><br><em>solo hoy</em> <del>antes $40</del> <code>ABC-1</code>"

    assert format_customer_text(text, "whatsapp") == "*Oferta*\n_solo hoy_ ~antes $40~ ```ABC-1```"


def test_whatsapp_formatter_strips_generic_html_wrappers():
    text = "<p><strong>Opciones</strong></p><ul><li>Pijama rosa</li><li>Set negro</li></ul>"

    assert format_customer_text(text, "whatsapp") == "*Opciones*\n- Pijama rosa\n- Set negro"


def test_whatsapp_formatter_preserves_formatting_markers_inside_urls():
    url = "https://store.example/catalogo_v1?promo=**sale**&tag=~~x~~"

    assert format_customer_text(f"Mira {url}", "whatsapp") == f"Mira {url}"


def test_whatsapp_formatter_preserves_line_breaks_and_lists():
    text = "**Opciones:**\n- Pijama rosada\n- Set negro"
    assert format_customer_text(text, "whatsapp") == "*Opciones:*\n- Pijama rosada\n- Set negro"


def test_whatsapp_formatter_breaks_inline_option_lists_into_lines():
    text = (
        "¡Súper! Para regalar en pijamas ahorita tenemos estas opciones: "
        "- Pijama rayas rosa $28 (tallas S, M, L) "
        "- Pijama amarilla $28 (tallas S, M, L, XXL) "
        "- Pijama satén azul $32 (tallas M, L, XL) "
        "¿Sabes qué talla usa tu novia? :)"
    )

    assert format_customer_text(text, "whatsapp") == (
        "¡Súper! Para regalar en pijamas ahorita tenemos estas opciones:\n"
        "- Pijama rayas rosa $28 (tallas S, M, L)\n"
        "- Pijama amarilla $28 (tallas S, M, L, XXL)\n"
        "- Pijama satén azul $32 (tallas M, L, XL)\n\n"
        "¿Sabes qué talla usa tu novia? :)"
    )


def test_whatsapp_formatter_does_not_split_plain_hyphen_phrases():
    text = "Tenemos pijamas - sets - lencería tipo encaje."

    assert format_customer_text(text, "whatsapp") == text


def test_instagram_formatter_removes_bold_markers_and_converts_lists():
    text = "**Opciones:**\n- Pijama rosada\n* Set negro\n1. Ver https://store.example/p*"
    formatted = format_customer_text(text, "instagram")
    assert formatted == "Opciones:\n• Pijama rosada\n• Set negro\n• Ver https://store.example/p*"
    assert "**" not in formatted
    assert "\n* Set" not in formatted


def test_instagram_formatter_removes_emojis_without_removing_spanish_punctuation():
    assert format_customer_text("¡Hola! Pijama azul 😊 💕", "instagram") == "¡Hola! Pijama azul"


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


def test_kommo_instagram_formatter_removes_emoji_even_when_preserve_is_configured():
    message, diagnostics = prepare_kommo_customer_message(
        "¡Hola! **Oferta** 💕",
        "instagram",
        {"kommo_emoji_mode_instagram": "preserve"},
    )

    assert message == "¡Hola! Oferta"
    assert diagnostics["emoji_present"] is False
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


def test_public_comment_formatter_removes_markdown_newlines_and_truncates():
    message, diagnostics = prepare_kommo_customer_message(
        "**Tenemos pijamas disponibles**\n- Escríbenos por DM para tallas y compra. " + "x" * 400,
        "instagram",
        {"kommo_emoji_mode_instagram": "preserve"},
        interaction_type="instagram_comment",
    )

    assert "**" not in message
    assert "\n" not in message
    assert len(message) <= 300
    assert diagnostics["interaction_type"] == "instagram_comment"
    assert diagnostics["channel"] == "instagram"


def test_safe_emoji_normalization_preserves_accents_punctuation_and_urls():
    message = normalize_emoji_for_kommo("¡Aquí está! 💕 https://store.example/promo?x=1&emoji=💕 😊")
    assert message == "¡Aquí está! ♡ https://store.example/promo?x=1&emoji=💕 :)"
    assert "?" in message
    assert "\ufffd" not in message


def test_emoji_stripping_removes_joined_sequences_cleanly():
    assert strip_emoji_characters("Promo 👩‍💻 lista") == "Promo lista"
