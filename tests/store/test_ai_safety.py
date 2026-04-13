from app.ai.engine import _clean_assistant_reply_text
from app.ai.prompts import _build_customer_context, build_system_prompt
from app.ai.safety import SAFE_FALLBACK_REPLY, sanitize_customer_facing_text
from app.broadcast.sender import _personalize_params
from app.customer_identity import extract_safe_first_name


def test_extract_safe_first_name_accepts_real_name():
    assert extract_safe_first_name("Luisana Pérez") == "Luisana"


def test_extract_safe_first_name_rejects_phrase_like_whatsapp_names():
    assert extract_safe_first_name("Dios es mi pastor") is None
    assert extract_safe_first_name("TODO POR MIS HIJOS") is None
    assert extract_safe_first_name("Mi Esposito 2") is None


def test_customer_context_only_exposes_safe_name_for_greeting():
    safe_context = _build_customer_context({"display_name": "Luisana Pérez"})
    unsafe_context = _build_customer_context({"display_name": "TODO POR MIS HIJOS"})

    assert "Nombre confirmado para saludar: Luisana" in safe_context
    assert "Nombre del perfil no confiable para saludar" in unsafe_context
    assert "TODO POR MIS HIJOS" not in unsafe_context


def test_customer_context_marks_pending_order_as_non_escalation_case():
    context = _build_customer_context(
        {"display_name": "Luisana Pérez"},
        open_order={
            "items": [{"product_name": "Set completo rojo", "quantity": 5}],
            "payment_status": "pending",
            "payment_method": "Zelle",
            "total": 175,
        },
    )

    assert "Pedido pendiente abierto" in context
    assert "NO es motivo para escalar" in context
    assert "Set completo rojo x5" in context


def test_sanitize_customer_facing_text_strips_leading_json_blob():
    raw = (
        '{"found": true, "count": 1, "products": [{"sku": "", "product_name": "Pijama satén azul"}]}\n'
        "¡Dale, Luisana! Para armarte las 5 de cada una solo me faltan las tallas."
    )

    cleaned = sanitize_customer_facing_text(raw)

    assert cleaned == "¡Dale, Luisana! Para armarte las 5 de cada una solo me faltan las tallas."


def test_sanitize_customer_facing_text_falls_back_when_json_survives():
    raw = 'Te comparto esto: {"found": true, "products": [{"sku": "ABC"}]}'

    assert sanitize_customer_facing_text(raw) == SAFE_FALLBACK_REPLY


def test_clean_assistant_reply_text_removes_tool_leak_and_sku(monkeypatch):
    raw = '{"sku":"ABC-123","found":true}\nDisponible el pijama satén azul ABC-123 en M.'
    monkeypatch.setattr("app.ai.engine.get_cached_catalog", lambda: [{"sku": "ABC-123"}])

    assert _clean_assistant_reply_text(raw) == "Disponible el pijama satén azul en M."


def test_broadcast_first_name_uses_safe_extraction():
    params = _personalize_params(["Hola {first_name}"], {"display_name": "TODO POR MIS HIJOS"})

    assert params == ["Hola Cliente"]


def test_system_prompt_includes_configured_discount_rule(monkeypatch):
    monkeypatch.setenv("DEBUG", "true")
    prompt = build_system_prompt(
        catalog_markdown="| Producto | Categoría | Tallas disponibles | Precio (USD) | Disponibilidad |\n|---|---|---|---|---|",
        payment_methods=[],
        accepted_exchange_rate="",
        order_discount_percent=15,
        order_discount_threshold_usd=500,
    )

    assert "15%" in prompt
    assert "$500" in prompt
