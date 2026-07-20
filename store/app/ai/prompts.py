"""
System prompt builder.
Loads the template from prompts/system_prompt.md and injects
the live product catalog and payment details at runtime.
"""

import json
from pathlib import Path
from app.catalog.sheets import group_catalog_products
from app.customer_identity import extract_safe_first_name
from app.payment_methods import payment_method_information_block, payment_method_names_text
from app.runtime_settings import RUNTIME_SETTING_DEFAULTS

# Resolve the prompts directory relative to the project root
_PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_template: str | None = None


def _load_template() -> str:
    """Load and cache the system prompt template. Uses SYSTEM_PROMPT_OVERRIDE if set."""
    global _template
    if _template is None:
        from app.config import get_config
        config = get_config()
        if config.system_prompt_override:
            _template = config.system_prompt_override
        else:
            path = _PROMPT_DIR / "system_prompt.md"
            _template = path.read_text(encoding="utf-8")
    return _template


def reload_template():
    """Force reload from disk (useful during development)."""
    global _template
    _template = None


def build_system_prompt(
    catalog_markdown: str,
    store_name: str = "Zona Pink",
    channel: str = "whatsapp",
    customer: dict | None = None,
    open_order: dict | None = None,
    payment_methods: list[dict] | None = None,
    accepted_exchange_rate: str | None = None,
    order_discount_percent: float | int | str | None = None,
    order_discount_threshold_usd: float | int | str | None = None,
    catalog_pdf_supported: bool | None = None,
) -> str:
    """
    Assemble the final system prompt by injecting the live product catalog,
    payment details, channel info, and customer context into the template.
    """
    template = _load_template()
    payment_methods_block = payment_method_information_block(payment_methods)
    payment_method_names = payment_method_names_text(payment_methods)
    exchange_rate_block = _build_exchange_rate_block(accepted_exchange_rate)
    order_discount_block = _build_order_discount_block(
        order_discount_percent=order_discount_percent,
        order_discount_threshold_usd=order_discount_threshold_usd,
    )

    prompt = template.format(
        store_name=store_name,
        product_catalog=catalog_markdown,
        payment_methods_block=payment_methods_block,
        payment_method_names_text=payment_method_names,
        exchange_rate_block=exchange_rate_block,
        order_discount_block=order_discount_block,
    )

    # Append channel-specific instructions
    channel_note = _build_channel_context(channel, catalog_pdf_supported=catalog_pdf_supported)
    if channel_note:
        prompt += f"\n\n# Canal actual\n\n{channel_note}"

    # Append customer context if we have history on them
    customer_note = _build_customer_context(customer, open_order=open_order)
    if customer_note:
        prompt += f"\n\n# Contexto del cliente\n\n{customer_note}"

    return prompt


def _build_exchange_rate_block(accepted_exchange_rate: str | None) -> str:
    rate_text = (accepted_exchange_rate or "").strip()
    if not rate_text:
        return (
            "No hay una tasa Binance del día configurada en este momento. "
            "Si el cliente pregunta por la tasa, explica que la tienda confirma "
            "la tasa Binance del día manualmente antes del pago y NO inventes un valor."
        )
    return (
        "La tienda usa como referencia la tasa Binance del día. "
        f"Valor configurado actualmente: {rate_text}. "
        "Si el cliente pregunta por la tasa, responde con este valor de forma directa y no digas que luego la vas a confirmar."
    )


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
            order_discount_threshold_usd
            if order_discount_threshold_usd is not None
            else default_threshold
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
            "Estás hablando por WhatsApp. "
            "Puedes usar send_interactive_buttons para mostrar opciones con botones. "
            "Úsalos solo cuando el cliente todavía no haya escogido una opción por texto. "
            "Los mensajes pueden ser más largos que en Instagram. "
            f"{pdf_note}"
        )
    elif channel == "instagram":
        return (
            "Estás hablando por Instagram DM. "
            "No puedes enviar botones interactivos (usa send_interactive_buttons igual, "
            "se convertirá automáticamente a Quick Replies). "
            "Mantén los mensajes más cortos (máximo 1000 bytes). "
            "NO puedes enviar mensajes proactivos: solo puedes responder dentro de "
            "las 24 horas después del último mensaje del cliente. "
            f"{pdf_note}"
        )
    return ""


def _build_customer_context(customer: dict | None, open_order: dict | None = None) -> str:
    """
    Build a brief context summary about the customer for the AI.
    Helps the AI personalize its responses without loading full history.
    """
    if not customer:
        return ""

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

    # Extract useful info from tags
    interests = [t.split(":")[1] for t in tags if t.startswith("interested:")]
    sizes = [t.split(":")[1] for t in tags if t.startswith("size:")]
    city = next((t.split(":")[1] for t in tags if t.startswith("city:")), None)

    if interests:
        parts.append(f"Intereses: {', '.join(interests)}")
    if sizes:
        parts.append(f"Tallas: {', '.join(sizes)}")
    if city:
        parts.append(f"Ciudad: {city}")

    parts.append("Importante: no asumas el método de pago por tags o compras anteriores; debes preguntarlo en la compra actual si el cliente aún no lo dijo.")

    last_addr = customer.get("last_shipping_address")
    if last_addr:
        parts.append(f"Última dirección de envío: {last_addr}")
        last_city = customer.get("last_shipping_city")
        if last_city:
            parts.append(f"Última ciudad: {last_city}")
        last_method = customer.get("last_shipping_method")
        if last_method:
            parts.append(f"Último método de envío: {last_method}")

    total_orders = customer.get("total_orders", 0)
    if total_orders > 0:
        parts.append(f"Pedidos anteriores: {total_orders}")
        parts.append(f"Total gastado: ${customer.get('total_spent', 0):.2f}")

    if open_order:
        open_items = open_order.get("items") or []
        if isinstance(open_items, str):
            open_items = json.loads(open_items or "[]")

        items_summary = ", ".join(
            f"{item.get('product_name', 'Producto')} x{item.get('quantity', 1)}"
            for item in open_items[:3]
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
            "Importante: este pedido pendiente NO es motivo para escalar. "
            "Si la cliente quiere retomarlo, ayúdala con ese pago. "
            "Si quiere comprar algo nuevo, maneja el nuevo flujo con claridad en el chat sin escalar."
        )

    if "vip" in tags:
        parts.append("⭐ Cliente VIP - trato especial")
    elif "repeat_buyer" in tags:
        parts.append("Cliente recurrente")
    elif "new_lead" in tags:
        parts.append("Cliente nuevo - primera conversación")

    return "\n".join(parts) if parts else ""


def format_catalog_as_markdown(products: list[dict]) -> str:
    """
    Convert a list of product dicts (from Google Sheets) into a markdown table
    for injection into the system prompt.

    Each product dict should have keys:
        sku, product_name, category, description, sizes, price_usd, stock, image_url
    """
    if not products:
        return "No hay productos disponibles en este momento."

    lines = [
        "| Producto | Categoría | Tallas disponibles | Precio (USD) | Disponibilidad |",
        "|----------|-----------|-------------------|--------------|----------------|",
    ]

    grouped_products = group_catalog_products(products)

    for p in grouped_products:
        try:
            in_stock = float(p.get("stock", 0) or 0) > 0
        except (TypeError, ValueError):
            in_stock = False
        availability = "Disponible" if in_stock else "Agotado"
        lines.append(
            f"| {p.get('product_name', '')} "
            f"| {p.get('category', '')} "
            f"| {p.get('sizes', '')} "
            f"| ${p.get('price_usd', 0):.2f} "
            f"| {availability} |"
        )

    return "\n".join(lines)
