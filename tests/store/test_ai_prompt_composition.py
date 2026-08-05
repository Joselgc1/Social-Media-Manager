from types import SimpleNamespace

import pytest
from app.ai import prompts


def _context(**overrides) -> prompts.PromptContext:
    values = {
        "catalog_markdown": "| Producto | Marca | Categoría | Presentaciones/opciones | Precio (USD) | Disponibilidad |\n| Pijama satén azul | Victoria's Secret | Pijamas | S,M | $28.00 | Disponible |",
        "store_name": "Tienda Rosa",
        "channel": "whatsapp",
        "customer": None,
        "open_order": None,
        "payment_methods": [{"id": "pm-zelle", "name": "Zelle", "information": "Correo: pagos@example.com"}],
        "accepted_exchange_rate": "40,25 Bs/USD",
        "exchange_rate_settings": None,
        "order_discount_percent": 10,
        "order_discount_threshold_usd": 350,
    }
    values.update(overrides)
    return prompts.PromptContext(**values)


def _normalize_prompt(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def test_fragment_loading():
    persona = prompts.load_prompt_file("shared/persona.md", use_cache=False)

    assert "# Identity and persona" in persona
    assert "{store_name}" in persona


def test_composition_order():
    template = prompts.compose_agent_template("legacy", use_cache=False)

    assert template.index("# Identity and persona") < template.index("# Core rules")
    assert template.index("# Core rules") < template.index("7. Keep responses SHORT")
    assert template.index("7. Keep responses SHORT") < template.index("10. When calling tag_customer")
    assert template.index("10. When calling tag_customer") < template.index("# Conversation flow")


def test_specialist_prompt_composition():
    sales = prompts.build_agent_prompt("sales", _context())
    checkout = prompts.build_agent_prompt("checkout", _context(workflow_state={"workflow_stage": "checkout_collecting"}))
    support = prompts.build_agent_prompt("support", _context())

    assert "# Sales Agent scope" in sales
    assert "request_agent_handoff" in sales
    assert "# Checkout Agent scope" in checkout
    assert "update_checkout_draft" in checkout
    assert "checkout_collecting" in checkout
    assert "Pijama satén azul" in checkout
    assert "# Support Agent scope" in support
    assert "get_customer_order_status" in support
    assert "Pijama satén azul" in support


def test_specialist_prompts_keep_legacy_business_rules():
    required_snippets = [
        "You ONLY discuss products listed in the PRODUCT CATALOG below",
        "NEVER reveal stock quantities",
        "do not restart every reply with \"hola\"",
        "NEVER output raw JSON",
        "NEVER assume the payment method from old tags",
        "Treat the conversation history as a real ongoing chat",
        "Never volunteer that you are an AI, bot, or automated system",
        "The payment method is the LAST checkout question",
        "Do not call BCV rates Binance",
        "Product prices do NOT include delivery",
        "Metro Valencia customers receive home delivery",
        "# PRODUCT CATALOG",
        "Pijama satén azul",
        "**Zelle**: Correo: pagos@example.com",
        "40,25 Bs por USD",
    ]

    for prompt_name in ("sales", "checkout", "support"):
        prompt = prompts.build_agent_prompt(prompt_name, _context())
        for snippet in required_snippets:
            assert snippet in prompt, f"{prompt_name} prompt missing: {snippet}"


def test_dynamic_context_injection():
    prompts.reload_template()
    prompt = prompts.build_legacy_prompt(
        _context(
            customer={
                "display_name": "Luisana Perez",
                "tags": ["interested:pajamas", "size:M", "city:caracas"],
                "last_fulfillment_type": "courier_agency_pickup",
                "last_shipping_city": "Caracas",
                "last_shipping_method": "mrw",
                "last_pickup_agency": "MRW Chacao",
                "total_orders": 1,
                "total_spent": 28,
            },
            open_order={
                "items": [{"product_name": "Pijama satén azul", "quantity": 1}],
                "payment_status": "pending",
                "payment_method": "Zelle",
                "total": 28,
            },
            workflow_state={"phase": "legacy"},
        )
    )

    assert "Tienda Rosa" in prompt
    assert "Pijama satén azul" in prompt
    assert "**Zelle**: Correo: pagos@example.com" in prompt
    assert "40,25 Bs por USD" in prompt
    assert "10%" in prompt
    assert "Nombre confirmado para saludar: Luisana" in prompt
    assert "Última agencia: MRW Chacao" in prompt
    assert "Último courier: mrw" in prompt
    assert "Pedido pendiente abierto" in prompt
    assert "estado de pago pending" in prompt
    assert "método de pago Zelle" in prompt
    assert "debes preguntarlo en la compra actual" in prompt
    assert "ayúdala con ese pago" in prompt
    assert "# Estado del flujo" in prompt
    assert '"phase": "legacy"' in prompt


def test_legacy_override_behavior(monkeypatch):
    prompts.reload_template()
    override = "OVERRIDE {store_name} {product_catalog} {payment_methods_block} {exchange_rate_block} {order_discount_block} {payment_method_names_text}"
    monkeypatch.setattr("app.config.get_config", lambda: SimpleNamespace(system_prompt_override=override))

    prompt = prompts.build_legacy_prompt(_context(channel="instagram"))

    assert prompt.startswith("OVERRIDE Tienda Rosa | Producto")
    assert "Correo: pagos@example.com" not in prompt
    assert "No proporciones datos ni instrucciones de pago" in prompt
    assert "40,25 Bs por USD" in prompt
    assert "# Canal actual" in prompt
    assert "Instagram DM" in prompt


@pytest.mark.parametrize("prompt_name", ["legacy", "sales", "support"])
def test_instagram_prompts_enforce_informational_channel_policy(prompt_name):
    common_customer = {
        "display_name": "Luisana Perez",
        "tags": ["interested:pajamas", "size:M", "city:caracas", "vip"],
        "last_shipping_city": "Caracas",
        "last_shipping_method": "mrw",
        "total_orders": 2,
        "total_spent": 120,
    }
    open_order = {
        "items": [{"product_name": "Pijama satén azul", "quantity": 1}],
        "payment_status": "pending",
        "payment_method": "Zelle",
        "total": 28,
    }
    home_prompt = prompts.build_agent_prompt(
        prompt_name,
        _context(
            channel="instagram",
            customer={
                **common_customer,
                "last_fulfillment_type": "home_delivery",
                "last_shipping_address": "Av Vieja, Casa 8",
                "last_shipping_zone": "Norte",
            },
            open_order=open_order,
        ),
    )
    pickup_prompt = prompts.build_agent_prompt(
        prompt_name,
        _context(
            channel="instagram",
            customer={
                **common_customer,
                "last_fulfillment_type": "courier_agency_pickup",
                "last_pickup_agency": "MRW Chacao",
            },
            open_order=open_order,
        ),
    )
    prompt = home_prompt + pickup_prompt

    assert "Instagram es un canal exclusivamente informativo" in prompt
    assert "No inicies ni continúes checkout" in prompt
    assert "no proporciones datos o instrucciones de pago" in prompt
    assert "continuamos las compras por WhatsApp" in prompt
    assert "llama send_whatsapp_handoff" in prompt
    assert "No redirijas a WhatsApp" in prompt
    assert "pagos@example.com" not in prompt
    assert "Nombre confirmado para saludar: Luisana" in prompt
    assert "Intereses: pajamas" in prompt
    assert "Tallas de ropa conocidas: M" in prompt
    assert "Ciudad: caracas" in prompt
    assert "Cliente VIP" in prompt
    assert "Av Vieja, Casa 8" not in prompt
    assert "Última zona: Norte" not in prompt
    assert "MRW Chacao" not in prompt
    assert "Último courier: mrw" not in prompt
    assert "Pedido pendiente abierto" not in prompt
    assert "estado de pago pending" not in prompt
    assert "método de pago Zelle" not in prompt
    assert "debes preguntarlo en la compra actual" not in prompt
    assert "ayúdala con ese pago" not in prompt


def test_cache_reload(monkeypatch, tmp_path):
    prompt_file = tmp_path / "shared" / "persona.md"
    prompt_file.parent.mkdir(parents=True)
    prompt_file.write_text("one", encoding="utf-8")
    monkeypatch.setattr(prompts, "_PROMPT_DIR", tmp_path)
    prompts.reload_template()

    assert prompts.load_prompt_file("shared/persona.md") == "one"
    prompt_file.write_text("two", encoding="utf-8")
    assert prompts.load_prompt_file("shared/persona.md") == "one"

    prompts.reload_template()
    assert prompts.load_prompt_file("shared/persona.md") == "two"


def test_missing_prompt_file(monkeypatch, tmp_path):
    monkeypatch.setattr(prompts, "_PROMPT_DIR", tmp_path)
    prompts.reload_template()

    with pytest.raises(prompts.PromptLoadError, match="Prompt file not found"):
        prompts.load_prompt_file("missing.md", use_cache=False)


def test_invalid_template_placeholders(monkeypatch, tmp_path):
    (tmp_path / "shared").mkdir()
    (tmp_path / "agents").mkdir()
    for fragment in prompts.LEGACY_SHARED_FRAGMENTS.values():
        (tmp_path / fragment).write_text("fragment", encoding="utf-8")
    (tmp_path / "agents" / "legacy.md").write_text("{persona}\n{missing_value}", encoding="utf-8")
    monkeypatch.setattr(prompts, "_PROMPT_DIR", tmp_path)
    prompts.reload_template()

    with pytest.raises(prompts.PromptRenderError, match="missing_value"):
        prompts.compose_agent_template("legacy", use_cache=False)


def test_semantic_parity_with_previous_prompt_builder(monkeypatch):
    monkeypatch.setattr("app.config.get_config", lambda: SimpleNamespace(system_prompt_override=""))
    prompts.reload_template()
    context = _context(channel="whatsapp")
    prompt = prompts.build_legacy_prompt(context)

    legacy_template = (prompts._PROMPT_DIR / "system_prompt.md").read_text(encoding="utf-8")
    values = prompts._build_template_values(context)
    expected = legacy_template.format(**values)
    expected += f"\n\n# Canal actual\n\n{prompts._build_channel_context('whatsapp')}"

    assert _normalize_prompt(prompt) == _normalize_prompt(expected)
