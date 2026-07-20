from app.channels.text_formatting import format_customer_text
from app.integrations.kommo.text_sanitizer import prepare_kommo_customer_message, strip_emoji_characters


def test_whatsapp_formatter_converts_markdown_bold_to_whatsapp_bold():
    assert format_customer_text(" **Oferta especial** ", "whatsapp") == "*Oferta especial*"


def test_instagram_formatter_preserves_channel_text():
    assert format_customer_text(" **Oferta especial** ", "instagram") == "**Oferta especial**"


def test_kommo_formatter_preserves_spanish_and_emoji_by_default():
    message, diagnostics = prepare_kommo_customer_message(
        "¡Hola! **Oferta** 💕\n¿Qué buscas?",
        "whatsapp",
        {"kommo_strip_emoji": False},
    )

    assert message == "¡Hola! *Oferta* 💕\n¿Qué buscas?"
    assert diagnostics == {
        "channel": "whatsapp",
        "message_length": len(message),
        "newline_count": 1,
        "non_ascii_present": True,
        "emoji_present": True,
        "replacement_char_present": False,
        "literal_question_mark_present": True,
        "kommo_strip_emoji_applied": False,
    }


def test_kommo_formatter_strips_emoji_without_replacement_characters():
    message, diagnostics = prepare_kommo_customer_message(
        "¡Hola **bella** 💕",
        "whatsapp",
        {"kommo_strip_emoji": True},
    )

    assert message == "¡Hola *bella*"
    assert "?" not in message
    assert "\ufffd" not in message
    assert diagnostics["emoji_present"] is False
    assert diagnostics["kommo_strip_emoji_applied"] is True


def test_emoji_stripping_removes_joined_sequences_cleanly():
    assert strip_emoji_characters("Promo 👩‍💻 lista") == "Promo lista"
