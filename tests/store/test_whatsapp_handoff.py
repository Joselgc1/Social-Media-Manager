from urllib.parse import parse_qs, urlparse

import pytest
from app.ai.tools import catalog as tool_catalog
from app.ai.tools.context import ToolExecutionContext
from app.ai.tools.executor import execute_tool
from app.ai.tools.messaging import (
    build_whatsapp_handoff_payload,
    normalize_whatsapp_phone_number,
)
from app.integrations.kommo.response_mapper import map_ai_response_to_salesbot


def _context(phone: str, channel: str = "instagram") -> ToolExecutionContext:
    return ToolExecutionContext(
        customer={"id": "customer-1", "platform_id": "ig-user"},
        channel=channel,
        store_phone_number=phone,
    )


def _catalog() -> list[dict]:
    return [{
        "sku": "PJ-001-M",
        "parent_sku": "PJ-001",
        "product_name": "Pijama Satinado",
        "category": "Pijamas",
        "description": "Pijama de satén",
        "size": "M",
        "sizes": "M",
        "price_usd": 28,
        "stock": 2,
        "image_url": "https://example.com/pijama.jpg",
    }]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("+58 (412) 123-4567", "584121234567"),
        ("0058 412 123 4567", "584121234567"),
        ("584121234567", "584121234567"),
    ],
)
def test_phone_number_normalization(value, expected):
    assert normalize_whatsapp_phone_number(value) == expected


@pytest.mark.parametrize("value", ["", "0412-1234567", "+58 call-me", "1234", "+58+4121234567"])
def test_invalid_phone_number_is_rejected(value):
    assert normalize_whatsapp_phone_number(value) is None


@pytest.mark.asyncio
async def test_handoff_builds_encoded_wa_url_with_safe_product_context(monkeypatch):
    monkeypatch.setattr(tool_catalog, "get_cached_catalog", _catalog)

    result = await execute_tool(
        "send_whatsapp_handoff",
        {
            "handoff_reason": "purchase",
            "product_name": "PJ-001-M",
            "size": "M",
            "quantity": 1,
            "sku": "SHOULD-NOT-LEAK",
        },
        _context("+58 (412) 123-4567"),
    )

    assert result["type"] == "whatsapp_handoff"
    parsed = urlparse(result["url"])
    assert parsed.scheme == "https"
    assert parsed.netloc == "wa.me"
    assert parsed.path == "/584121234567"
    message = parse_qs(parsed.query)["text"][0]
    assert message == (
        "Hola, vengo de Instagram y quiero comprar Pijama Satinado, talla M, cantidad 1. "
        "¿Me pueden ayudar con el pedido?"
    )
    assert "%C2%BFMe%20pueden%20ayudar" in result["url"]
    assert "PJ-001-M" not in result["url"]
    assert "SHOULD-NOT-LEAK" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("phone", ["", "0412-1234567"])
async def test_handoff_missing_or_invalid_phone_returns_natural_fallback(phone, caplog):
    result = await execute_tool(
        "send_whatsapp_handoff",
        {"handoff_reason": "purchase"},
        _context(phone),
    )

    assert result["type"] == "whatsapp_handoff"
    assert result["url"] is None
    assert "contacto de WhatsApp disponible en el perfil" in result["customer_text"]
    assert "store_phone_number is missing or invalid" in caplog.text


@pytest.mark.asyncio
async def test_handoff_handler_rejects_non_instagram_channel():
    result = await execute_tool(
        "send_whatsapp_handoff",
        {"handoff_reason": "purchase"},
        _context("+58 412 1234567", channel="whatsapp"),
    )

    assert result["status"] == "error"
    assert "not allowed" in result["message"]


def test_kommo_mapper_keeps_clickable_handoff_url_once():
    url = "https://wa.me/584121234567?text=Hola%20Instagram"
    output = map_ai_response_to_salesbot({
        "text": f"Continuamos por WhatsApp:\n{url}",
        "whatsapp_handoff": {"type": "whatsapp_handoff", "url": url},
    })

    assert output.discarded is False
    assert output.customer_text.count(url) == 1


def test_image_delivery_failure_handoff_acknowledges_failure_and_links_to_whatsapp():
    result = build_whatsapp_handoff_payload(
        reason="product_image_delivery_failed",
        store_phone_number="+58 412 1234567",
        product_name="Acqua di Gio",
    )

    assert result["customer_text"] == (
        "Parece que hay un error aquí en Instagram y no puedo enviarte la imagen correctamente. "
        "Intenta escribirnos por WhatsApp aquí y seguro te ayudamos:\n"
        f"{result['url']}"
    )
    assert result["display_url"] in result["customer_text"]
    assert "?text=" in result["customer_text"]
    parsed = urlparse(result["url"])
    assert parsed.path == "/584121234567"
    assert parse_qs(parsed.query)["text"] == [
        "Hola, quiero la foto de Acqua di Gio."
    ]
