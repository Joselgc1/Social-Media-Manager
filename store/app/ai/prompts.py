"""
System prompt builder.
Loads the template from prompts/system_prompt.md and injects
the live product catalog and payment details at runtime.
"""

import json
import os
from pathlib import Path

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
    store_name: str = "Tu Tienda VS",
    channel: str = "whatsapp",
    customer: dict | None = None,
    zelle_details: str = "[Configurar en .env]",
    binance_details: str = "[Configurar en .env]",
    zinli_details: str = "[Configurar en .env]",
    bolivares_details: str = "[Configurar en .env]",
) -> str:
    """
    Assemble the final system prompt by injecting the live product catalog,
    payment details, channel info, and customer context into the template.
    """
    template = _load_template()

    prompt = template.format(
        store_name=store_name,
        product_catalog=catalog_markdown,
        zelle_details=zelle_details,
        binance_details=binance_details,
        zinli_details=zinli_details,
        bolivares_details=bolivares_details,
    )

    # Append channel-specific instructions
    channel_note = _build_channel_context(channel)
    if channel_note:
        prompt += f"\n\n# Canal actual\n\n{channel_note}"

    # Append customer context if we have history on them
    customer_note = _build_customer_context(customer)
    if customer_note:
        prompt += f"\n\n# Contexto del cliente\n\n{customer_note}"

    return prompt


def _build_channel_context(channel: str) -> str:
    """Return channel-specific instructions for the AI."""
    if channel == "whatsapp":
        return (
            "Estás hablando por WhatsApp. "
            "Puedes usar send_interactive_buttons para mostrar opciones con botones. "
            "Los mensajes pueden ser más largos que en Instagram."
        )
    elif channel == "instagram":
        return (
            "Estás hablando por Instagram DM. "
            "No puedes enviar botones interactivos (usa send_interactive_buttons igual, "
            "se convertirá automáticamente a Quick Replies). "
            "Mantén los mensajes más cortos (máximo 1000 bytes). "
            "NO puedes enviar mensajes proactivos: solo puedes responder dentro de "
            "las 24 horas después del último mensaje del cliente."
        )
    return ""


def _build_customer_context(customer: dict | None) -> str:
    """
    Build a brief context summary about the customer for the AI.
    Helps the AI personalize its responses without loading full history.
    """
    if not customer:
        return ""

    parts = []

    name = customer.get("display_name")
    if name:
        parts.append(f"Nombre: {name}")

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
        "| SKU | Producto | Categoría | Tallas disponibles | Precio (USD) | Stock |",
        "|-----|----------|-----------|-------------------|--------------|-------|",
    ]

    for p in products:
        lines.append(
            f"| {p.get('sku', '')} "
            f"| {p.get('product_name', '')} "
            f"| {p.get('category', '')} "
            f"| {p.get('sizes', '')} "
            f"| ${p.get('price_usd', 0):.2f} "
            f"| {p.get('stock', 0)} |"
        )

    return "\n".join(lines)
