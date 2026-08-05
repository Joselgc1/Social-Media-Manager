"""
Composable system prompt builder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ai.policies.channel_capabilities import get_channel_capabilities
from app.catalog.sheets import group_catalog_products
from app.customer_identity import extract_safe_first_name
from app.exchange_rates import build_exchange_rate_prompt_block
from app.payment_methods import payment_method_information_block, payment_method_names_text
from app.runtime_settings import RUNTIME_SETTING_DEFAULTS

_PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_template: str | None = None
_prompt_file_cache: dict[Path, str] = {}

LEGACY_PROMPT_NAME = "legacy"
PROMPT_SHARED_FRAGMENTS = {
    "persona": "shared/persona.md",
    "safety_rules": "shared/safety_rules.md",
    "communication_style": "shared/communication_style.md",
    "channel_rules": "shared/channel_rules.md",
    "conversation_flow": "shared/conversation_flow.md",
    "business_context": "shared/business_context.md",
}
LEGACY_SHARED_FRAGMENTS = PROMPT_SHARED_FRAGMENTS
KNOWN_AGENT_PROMPTS = {"legacy", "sales", "checkout", "support"}
DYNAMIC_TEMPLATE_PLACEHOLDERS = (
    "store_name",
    "product_catalog",
    "payment_methods_block",
    "payment_method_names_text",
    "exchange_rate_block",
    "order_discount_block",
)


class PromptLoadError(RuntimeError):
    """Raised when a prompt file cannot be loaded."""


class PromptRenderError(RuntimeError):
    """Raised when a prompt template cannot be rendered."""


@dataclass(frozen=True)
class PromptContext:
    """Dynamic values used to render an agent prompt."""

    catalog_markdown: str
    store_name: str = "Zona Pink"
    channel: str = "whatsapp"
    customer: dict[str, Any] | None = None
    open_order: dict[str, Any] | None = None
    payment_methods: list[dict[str, Any]] | None = None
    accepted_exchange_rate: str | None = None
    exchange_rate_settings: dict[str, Any] | None = None
    order_discount_percent: float | int | str | None = None
    order_discount_threshold_usd: float | int | str | None = None
    catalog_pdf_supported: bool | None = None
    workflow_state: dict[str, Any] | str | None = None
    instagram_content_context: dict[str, Any] | None = None


def load_prompt_file(relative_path: str, *, use_cache: bool = True) -> str:
    """Load a prompt file relative to the prompt directory."""
    path = (_PROMPT_DIR / relative_path).resolve()
    try:
        path.relative_to(_PROMPT_DIR.resolve())
    except ValueError as exc:
        raise PromptLoadError(f"Prompt path escapes prompt directory: {relative_path}") from exc

    if use_cache and path in _prompt_file_cache:
        return _prompt_file_cache[path]

    if not path.exists():
        raise PromptLoadError(f"Prompt file not found: {path}")
    if not path.is_file():
        raise PromptLoadError(f"Prompt path is not a file: {path}")

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptLoadError(f"Could not read prompt file {path}: {exc}") from exc

    if use_cache:
        _prompt_file_cache[path] = text
    return text


def compose_agent_template(prompt_name: str, *, use_cache: bool = True) -> str:
    """Compose an agent template from shared prompt fragments."""
    if prompt_name not in KNOWN_AGENT_PROMPTS:
        raise PromptLoadError(f"Unknown prompt template: {prompt_name}")

    fragments = {
        key: load_prompt_file(relative_path, use_cache=use_cache).strip()
        for key, relative_path in PROMPT_SHARED_FRAGMENTS.items()
    }
    values = {
        **fragments,
        **{key: "{" + key + "}" for key in DYNAMIC_TEMPLATE_PLACEHOLDERS},
    }
    template = load_prompt_file(f"agents/{prompt_name}.md", use_cache=use_cache)
    return _render_template(template, values, template_name=f"agents/{prompt_name}.md")


def _load_template() -> str:
    """Load and cache the legacy prompt template. Uses SYSTEM_PROMPT_OVERRIDE if set."""
    global _template
    if _template is None:
        from app.config import get_config

        config = get_config()
        _template = config.system_prompt_override or load_prompt_file("system_prompt.md")
    return _template


def reload_template() -> None:
    """Force reload from disk or environment override on the next build."""
    global _template, _prompt_file_cache
    _template = None
    _prompt_file_cache = {}


def build_legacy_prompt(context: PromptContext) -> str:
    """Build the currently active legacy-agent prompt."""
    template = _load_template()
    values = _build_template_values(context)
    prompt = _render_template(template, values, template_name=LEGACY_PROMPT_NAME)

    channel_note = _build_channel_context(context.channel, catalog_pdf_supported=context.catalog_pdf_supported)
    if channel_note:
        prompt += f"\n\n# Canal actual\n\n{channel_note}"

    customer_note = _build_customer_context(context.customer, open_order=context.open_order, channel=context.channel)
    if customer_note:
        prompt += f"\n\n# Contexto del cliente\n\n{customer_note}"

    workflow_note = _build_workflow_context(context.workflow_state)
    if workflow_note:
        prompt += f"\n\n# Estado del flujo\n\n{workflow_note}"

    instagram_note = _build_instagram_content_context(context.instagram_content_context)
    if instagram_note:
        prompt += f"\n\n# Contexto privado de contenido de Instagram\n\n{instagram_note}"

    return prompt


def build_agent_prompt(prompt_name: str, context: PromptContext) -> str:
    """Build a prompt for a registered agent prompt name."""
    if prompt_name == LEGACY_PROMPT_NAME:
        return build_legacy_prompt(context)
    if prompt_name not in KNOWN_AGENT_PROMPTS:
        raise PromptLoadError(f"Unknown prompt template: {prompt_name}")

    template = compose_agent_template(prompt_name)
    values = _build_template_values(context)
    prompt = _render_template(template, values, template_name=prompt_name)

    channel_note = _build_channel_context(context.channel, catalog_pdf_supported=context.catalog_pdf_supported)
    if channel_note:
        prompt += f"\n\n# Canal actual\n\n{channel_note}"

    customer_note = _build_customer_context(context.customer, open_order=context.open_order, channel=context.channel)
    if customer_note:
        prompt += f"\n\n# Contexto del cliente\n\n{customer_note}"

    workflow_note = _build_workflow_context(context.workflow_state)
    if workflow_note:
        prompt += f"\n\n# Estado del flujo\n\n{workflow_note}"

    instagram_note = _build_instagram_content_context(context.instagram_content_context)
    if instagram_note:
        prompt += f"\n\n# Contexto privado de contenido de Instagram\n\n{instagram_note}"

    return prompt


def build_system_prompt(
    catalog_markdown: str,
    store_name: str = "Zona Pink",
    channel: str = "whatsapp",
    customer: dict | None = None,
    open_order: dict | None = None,
    payment_methods: list[dict] | None = None,
    accepted_exchange_rate: str | None = None,
    exchange_rate_settings: dict[str, Any] | None = None,
    order_discount_percent: float | int | str | None = None,
    order_discount_threshold_usd: float | int | str | None = None,
    workflow_state: dict[str, Any] | str | None = None,
    catalog_pdf_supported: bool | None = None,
) -> str:
    """Assemble the final system prompt with live store context."""
    return build_legacy_prompt(
        PromptContext(
            catalog_markdown=catalog_markdown,
            store_name=store_name,
            channel=channel,
            customer=customer,
            open_order=open_order,
            payment_methods=payment_methods,
            accepted_exchange_rate=accepted_exchange_rate,
            exchange_rate_settings=exchange_rate_settings,
            order_discount_percent=order_discount_percent,
            order_discount_threshold_usd=order_discount_threshold_usd,
            catalog_pdf_supported=catalog_pdf_supported,
            workflow_state=workflow_state,
        )
    )


def _build_template_values(context: PromptContext) -> dict[str, Any]:
    capabilities = get_channel_capabilities(context.channel)
    if capabilities.informational_only:
        payment_methods_block = "No proporciones datos ni instrucciones de pago en este canal."
        payment_method_names = "no disponibles para checkout en este canal"
    else:
        payment_methods_block = payment_method_information_block(context.payment_methods)
        payment_method_names = payment_method_names_text(context.payment_methods)
    exchange_rate_block = _build_exchange_rate_block(context.exchange_rate_settings, context.accepted_exchange_rate)
    order_discount_block = _build_order_discount_block(
        order_discount_percent=context.order_discount_percent,
        order_discount_threshold_usd=context.order_discount_threshold_usd,
    )

    return {
        "store_name": context.store_name,
        "product_catalog": context.catalog_markdown,
        "payment_methods_block": payment_methods_block,
        "payment_method_names_text": payment_method_names,
        "exchange_rate_block": exchange_rate_block,
        "order_discount_block": order_discount_block,
    }


def _render_template(template: str, values: dict[str, Any], *, template_name: str) -> str:
    try:
        return template.format(**values)
    except KeyError as exc:
        missing = exc.args[0]
        raise PromptRenderError(f"Missing template value '{missing}' while rendering {template_name}") from exc
    except (IndexError, ValueError) as exc:
        raise PromptRenderError(f"Invalid template placeholders while rendering {template_name}: {exc}") from exc


def _build_exchange_rate_block(
    exchange_rate_settings: dict[str, Any] | None,
    accepted_exchange_rate: str | None,
) -> str:
    settings = exchange_rate_settings
    if settings is None and accepted_exchange_rate:
        settings = {"exchange_rate_reference": "manual", "manual_exchange_rate": accepted_exchange_rate}
    return build_exchange_rate_prompt_block(settings, accepted_exchange_rate)


def _build_order_discount_block(
    order_discount_percent: float | int | str | None,
    order_discount_threshold_usd: float | int | str | None,
) -> str:
    default_percent = float(RUNTIME_SETTING_DEFAULTS["order_discount_percent"])
    default_threshold = float(RUNTIME_SETTING_DEFAULTS["order_discount_threshold_usd"])

    try:
        percent = float(order_discount_percent if order_discount_percent is not None else default_percent)
    except (TypeError, ValueError):
        percent = default_percent
    try:
        threshold = float(
            order_discount_threshold_usd if order_discount_threshold_usd is not None else default_threshold
        )
    except (TypeError, ValueError):
        threshold = default_threshold

    if percent <= 0 or threshold <= 0:
        return (
            "No hay descuento automático configurado en este momento. "
            "No prometas descuentos salvo que la tienda lo confirme manualmente."
        )

    percent_text = f"{percent:.2f}".rstrip("0").rstrip(".")
    threshold_text = f"{threshold:.2f}".rstrip("0").rstrip(".")
    return (
        f"Si el subtotal de productos de un pedido es MAYOR a ${threshold_text}, "
        f"aplica un descuento automático de {percent_text}% sobre ese subtotal. "
        "Shipping se cobra aparte. No inventes precios unitarios rebajados: menciona el descuento solo sobre el total cuando corresponda."
    )


def _build_channel_context(channel: str, catalog_pdf_supported: bool | None = None) -> str:
    """Return channel-specific instructions for the AI."""
    pdf_supported = channel == "whatsapp" if catalog_pdf_supported is None else catalog_pdf_supported
    pdf_note = (
        "Si el cliente pide el catálogo completo, puedes usar send_catalog_pdf para enviar el PDF."
        if pdf_supported
        else (
            "No puedes enviar ni prometer un PDF del catálogo en este canal. "
            "Si el cliente pide catálogo, responde en texto con las categorías disponibles, "
            "recomienda opciones relevantes si aplica y haz una pregunta útil para continuar."
        )
    )
    if channel == "whatsapp":
        return (
            "Estás hablando por WhatsApp. Puedes usar send_interactive_buttons para mostrar opciones con botones. "
            "Úsalos solo cuando el cliente todavía no haya escogido una opción por texto. "
            f"Los mensajes pueden ser más largos que en Instagram. {pdf_note}"
        )
    if channel == "instagram":
        return (
            "Estás hablando por Instagram DM. No puedes enviar botones interactivos (usa send_interactive_buttons igual, "
            "se convertirá automáticamente a Quick Replies). Mantén los mensajes más cortos (máximo 1000 bytes). "
            "NO puedes enviar mensajes proactivos: solo puedes responder dentro de las 24 horas después del último mensaje del cliente. "
            "Instagram es un canal exclusivamente informativo: responde normalmente preguntas de productos, recomendaciones, "
            "precios, disponibilidad, presentaciones/opciones, comparaciones e imágenes. No inicies ni continúes checkout, "
            "no crees, modifiques, confirmes o canceles pedidos, y no solicites direcciones, agencias de entrega ni métodos de pago "
            "para un pedido. No proceses comprobantes, no proporciones datos o instrucciones de pago y no hagas handoff interno "
            "a Checkout ni Payment. Cuando la cliente claramente quiera comprar, pagar, hacer un pedido o recibir el catálogo PDF, "
            "llama send_whatsapp_handoff. Para compras, usa lenguaje de servicio como: 'Para ayudarte mejor con el pedido, el pago "
            "y el envío, continuamos las compras por WhatsApp.' Si pide el PDF, llama send_whatsapp_handoff con "
            "handoff_reason='catalog_pdf' y explica que se entrega por WhatsApp; nunca llames send_catalog_pdf en Instagram. "
            "Puedes responder primero cualquier pregunta informativa útil y luego hacer el handoff. No redirijas a WhatsApp a quien "
            "solo esté explorando o haciendo preguntas sobre productos. No vuelvas a insistir con WhatsApp si la cliente rechaza el "
            "cambio de canal o cierra la conversación."
        )
    return ""


def _build_customer_context(customer: dict | None, open_order: dict | None = None, channel: str = "whatsapp") -> str:
    """Build a brief safe context summary about the customer."""
    if not customer:
        return ""

    informational_only = get_channel_capabilities(channel).informational_only
    parts = []

    name = customer.get("display_name")
    safe_first_name = extract_safe_first_name(name)
    if safe_first_name:
        parts.append(f"Nombre confirmado para saludar: {safe_first_name}")
    elif name:
        parts.append(
            "Nombre del perfil no confiable para saludar: no uses ese nombre "
            "a menos que la cliente lo confirme claramente en el chat."
        )

    tags = customer.get("tags") or []
    if isinstance(tags, str):
        tags = json.loads(tags)

    interests = [tag.split(":")[1] for tag in tags if tag.startswith("interested:")]
    sizes = [tag.split(":")[1] for tag in tags if tag.startswith("size:")]
    city = next((tag.split(":")[1] for tag in tags if tag.startswith("city:")), None)

    if interests:
        parts.append(f"Intereses: {', '.join(interests)}")
    if sizes:
        parts.append(f"Tallas de ropa conocidas: {', '.join(sizes)}")
    if city:
        parts.append(f"Ciudad: {city}")

    if not informational_only:
        parts.append(
            "Importante: no asumas el método de pago por tags o compras anteriores; "
            "debes preguntarlo en la compra actual si el cliente aún no lo dijo."
        )
        last_addr = customer.get("last_shipping_address")
        last_city = customer.get("last_shipping_city")
        last_fulfillment_type = customer.get("last_fulfillment_type")
        if last_fulfillment_type == "home_delivery" and last_addr:
            parts.append(f"Última entrega: domicilio en {last_city or 'ciudad no registrada'}")
            parts.append(f"Última dirección de envío: {last_addr}")
            if customer.get("last_shipping_zone"):
                parts.append(f"Última zona: {customer['last_shipping_zone']}")
        elif last_fulfillment_type == "courier_agency_pickup" and customer.get("last_pickup_agency"):
            parts.append(f"Última entrega: retiro en agencia en {last_city or 'ciudad no registrada'}")
            parts.append(f"Última agencia: {customer['last_pickup_agency']}")
            if customer.get("last_shipping_method"):
                parts.append(f"Último courier: {customer['last_shipping_method']}")
        elif last_addr:
            parts.append(f"Última dirección de envío: {last_addr}")
            if last_city:
                parts.append(f"Última ciudad: {last_city}")
            if customer.get("last_shipping_method"):
                parts.append(f"Último método de envío: {customer['last_shipping_method']}")

        total_orders = customer.get("total_orders", 0)
        if total_orders > 0:
            parts.append(f"Pedidos anteriores: {total_orders}")
            parts.append(f"Total gastado: ${customer.get('total_spent', 0):.2f}")

        if open_order:
            open_items = open_order.get("items") or []
            if isinstance(open_items, str):
                open_items = json.loads(open_items or "[]")
            items_summary = ", ".join(
                f"{item.get('product_name', 'Producto')} x{item.get('quantity', 1)}" for item in open_items[:3]
            )
            parts.append(
                "Pedido pendiente abierto: "
                f"estado de pago {open_order.get('payment_status', 'pending')}, "
                f"método de pago {open_order.get('payment_method') or 'sin definir'}, "
                f"total ${float(open_order.get('total') or 0):.2f}."
            )
            if items_summary:
                parts.append(f"Resumen pedido pendiente: {items_summary}")
            parts.append(
                "Importante: este pedido pendiente NO es motivo para escalar. Si la cliente quiere retomarlo, "
                "ayúdala con ese pago. Si quiere comprar algo nuevo, maneja el nuevo flujo con claridad en el chat sin escalar."
            )

    if "vip" in tags:
        parts.append("⭐ Cliente VIP - trato especial")
    elif "repeat_buyer" in tags:
        parts.append("Cliente recurrente")
    elif "new_lead" in tags:
        parts.append("Cliente nuevo - primera conversación")

    return "\n".join(parts) if parts else ""


def _build_workflow_context(workflow_state: dict[str, Any] | str | None) -> str:
    """Render optional future workflow state without affecting the legacy default."""
    if not workflow_state:
        return ""
    if isinstance(workflow_state, str):
        return workflow_state.strip()
    return json.dumps(workflow_state, ensure_ascii=False, sort_keys=True)


def _build_instagram_content_context(context: dict[str, Any] | None) -> str:
    if not context or context.get("source") != "story_reply":
        return ""
    products = context.get("products") if isinstance(context.get("products"), list) else []
    if not products:
        return ""
    selected = context.get("selected_product_sku")
    lines = [
        "La cliente respondió en privado a una Historia de Instagram asociada a los productos siguientes.",
        "Los precios, presentaciones/opciones y disponibilidad indicados aquí vienen del catálogo en vivo.",
    ]
    for product in products:
        if not isinstance(product, dict):
            continue
        lines.append(
            "- "
            + str(product.get("name") or "Producto")
            + f" [SKU interno {product.get('sku')}]: precio {product.get('price_text')}, "
            + f"presentaciones {product.get('sizes') or 'por confirmar'}, "
            + f"disponibilidad {product.get('availability')}"
        )
    if selected:
        lines.append(
            f"Producto seleccionado para esta conversación: SKU interno {selected}. "
            "Interpreta preguntas breves como precio, presentación o disponibilidad sobre ese producto."
        )
    elif len(products) > 1:
        lines.append(
            "Hay varios productos asociados. Limita la interpretación a esta lista y pide una aclaración breve "
            "si el mensaje no identifica uno; no elijas ni adivines."
        )
    lines.append(
        "Si la cliente identifica claramente otro producto del catálogo en su mensaje actual, ese producto explícito "
        "reemplaza este contexto temporal. Nunca reveles SKUs internos ni cantidades exactas de inventario."
    )
    return "\n".join(lines)


def format_catalog_as_markdown(products: list[dict]) -> str:
    """Convert live catalog data into a generic retail markdown table for the LLM."""
    if not products:
        return "No hay productos disponibles en este momento."

    lines = [
        "| Producto | Marca | Categoría | Presentaciones/opciones | Precio (USD) | Disponibilidad |",
        "|----------|-------|-----------|-------------------------|--------------|----------------|",
    ]

    for product in group_catalog_products(products):
        try:
            in_stock = float(product.get("stock", 0) or 0) > 0
        except (TypeError, ValueError):
            in_stock = False
        availability = "Disponible" if in_stock else "Agotado"
        lines.append(
            f"| {product.get('product_name', '')} "
            f"| {product.get('brand', '')} "
            f"| {product.get('category', '')} "
            f"| {product.get('presentations', '') or 'Única'} "
            f"| {_catalog_price_text(product)} "
            f"| {availability} |"
        )

    return "\n".join(lines)


def _catalog_price_text(product: dict) -> str:
    """Expose exact variant prices when a grouped product has different prices."""
    if product.get("has_variant_prices"):
        parts = []
        for variant in product.get("variants", []):
            if not variant.get("in_stock"):
                continue
            try:
                price = f"${float(variant.get('price_usd', 0)):.2f}"
            except (TypeError, ValueError):
                continue
            presentation = str(variant.get("size") or "").strip()
            parts.append(f"{presentation}: {price}" if presentation else price)
        return "; ".join(parts) or "Por confirmar"
    try:
        return f"${float(product.get('price_usd', 0)):.2f}"
    except (TypeError, ValueError):
        return "Por confirmar"
