"""
Telegram admin bot.
Lets the store owner manage the chatbot from Telegram using simple commands.
Runs as a webhook receiver inside the same FastAPI app.

Commands:
    /start          - Welcome message with command list
    /stats          - Today's conversation and order stats
    /customers      - Recent customers with tags
    /orders         - Recent orders and their status
    /order ID STATUS - Update an order's status
    /resolve ID     - Resolve an escalated conversation (return to AI)
    /provider NAME  - Switch LLM provider (openai/anthropic)
    /broadcast      - List recent broadcasts
    /send ID        - Execute a draft/scheduled broadcast immediately
    /preview TAGS   - Preview how many customers match tag filters
    /settings       - View current settings
    /usage          - Today's LLM token usage and cost
"""

import hmac
import json
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

from app import db
from app.admin.customer_activation import ManualActivationError, activate_customer_for_admin
from app.admin.notify import notify_owner
from app.admin.telegram_sender import escape_markdown
from app.ai.providers import AVAILABLE_MODELS, get_model_costs
from app.analytics import get_conversion_funnel, get_popular_products, get_response_time_stats
from app.broadcast.sender import execute_broadcast, list_broadcasts, preview_broadcast
from app.catalog.pdf_generator import generate_catalog_pdf, get_pdf_metadata
from app.catalog.sheets import get_cached_catalog
from app.config import get_config
from app.crm import orders
from app.crm.customers import add_tags, remove_tag
from app.exchange_rates import format_rate_for_customer, selected_exchange_rate
from app.log_redaction import install_secret_redaction_filter
from app.request_limits import limiter

logger = logging.getLogger(__name__)
router = APIRouter()


# -- Webhook endpoint ------------------------------------------------

@router.post("/webhooks/telegram")
@limiter.exempt
async def handle_telegram(request: Request):
    """
    Receive Telegram updates via webhook.
    Only processes messages from the configured admin chat ID.
    """
    config = get_config()
    provided_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not config.telegram_webhook_secret or not hmac.compare_digest(
        provided_secret,
        config.telegram_webhook_secret,
    ):
        raise HTTPException(status_code=403, detail="Forbidden")

    body = await request.json()

    message = body.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = message.get("text", "").strip()

    # Only allow the configured admin
    if chat_id != config.telegram_admin_chat_id:
        return Response(status_code=200)

    if not text.startswith("/"):
        return Response(status_code=200)

    # Parse command and arguments
    parts = text.split(maxsplit=1)
    command = parts[0].lower().split("@")[0]  # Remove @botname suffix
    args = parts[1] if len(parts) > 1 else ""

    # Route to handler
    response_text = await _handle_command(command, args)

    if response_text:
        await notify_owner(response_text)

    return Response(status_code=200)


# -- Command handlers ------------------------------------------------

async def _handle_command(command: str, args: str) -> str:
    try:
        if command == "/start":
            return _cmd_start()
        elif command == "/stats":
            return await _cmd_stats()
        elif command == "/customers":
            return await _cmd_customers(args)
        elif command == "/orders":
            return await _cmd_orders()
        elif command == "/order":
            return await _cmd_update_order(args)
        elif command == "/resolve":
            return await _cmd_resolve(args)
        elif command == "/provider":
            return await _cmd_switch_provider(args)
        elif command == "/broadcast":
            return await _cmd_list_broadcasts()
        elif command == "/send":
            return await _cmd_send_broadcast(args)
        elif command == "/preview":
            return await _cmd_preview(args)
        elif command == "/settings":
            return await _cmd_settings()
        elif command == "/usage":
            return await _cmd_usage()
        elif command == "/conversion":
            return await _cmd_conversion(args)
        elif command == "/performance":
            return await _cmd_performance(args)
        elif command == "/products":
            return await _cmd_popular_products()
        elif command == "/catalogpdf":
            return await _cmd_generate_catalog_pdf()
        elif command == "/ai":
            return await _cmd_ai_toggle(args)
        elif command == "/tags":
            return await _cmd_tags(args)
        elif command == "/tag":
            return await _cmd_tag_edit(args)
        else:
            return f"Comando desconocido: {command}\nUsa /start para ver los comandos disponibles."
    except Exception as e:
        logger.exception(f"Telegram command error: {command} {args}")
        return f"Error procesando comando: {e}"


def _cmd_start() -> str:
    return (
        "🤖 *VS Chatbot Admin*\n\n"
        "Comandos disponibles:\n\n"
        "📊 *Monitoreo*\n"
        "/stats - Estadísticas de hoy\n"
        "/usage - Uso de tokens y costos\n"
        "/settings - Ver configuración actual\n\n"
        "👥 *Clientes*\n"
        "/customers - Clientes recientes\n"
        "/customers vip - Filtrar por tag\n"
        "/resolve - Ver clientes escalados\n"
        "/resolve ID - Resolver uno\n"
        "/resolve all - Resolver todos\n"
        "/tags ID - Ver tags de un cliente\n"
        "/tag ID add tag1,tag2 - Agregar tags\n"
        "/tag ID del tag1 - Eliminar un tag\n\n"
        "📦 *Pedidos*\n"
        "/orders - Pedidos recientes\n"
        "/order ID confirmed - Actualizar estado\n\n"
        "📢 *Broadcasts*\n"
        "/broadcast - Ver broadcasts\n"
        "/send ID - Enviar broadcast\n"
        "/preview tag1,tag2 - Vista previa\n\n"
        "⚙️ *Configuración*\n"
        "/provider openai - Cambiar proveedor\n"
        "/provider anthropic - Cambiar proveedor\n\n"
        "📈 *Analíticas*\n"
        "/conversion - Embudo de conversión\n"
        "/performance - Tiempos de respuesta\n"
        "/products - Productos populares\n"
        "\n"
        "📄 *Catálogo*\n"
        "/catalogpdf - Generar PDF del catálogo\n\n"
        "🤖 *Control del AI*\n"
        "/ai on - Activar respuestas automáticas\n"
        "/ai off - Pausar AI (responder manualmente)"
    )


async def _cmd_stats() -> str:
    # Conversation stats
    conv_rows = await db.fetch_all(
        """
        SELECT channel,
               COUNT(DISTINCT customer_id) as customers,
               COUNT(*) FILTER (WHERE role = 'user') as msgs_in,
               COUNT(*) FILTER (WHERE role = 'assistant') as msgs_out
        FROM conversations
        WHERE created_at >= CURRENT_DATE
        GROUP BY channel
        """
    )

    conv_lines = []
    total_in = 0
    total_out = 0
    for r in conv_rows:
        conv_lines.append(f"  {r['channel']}: {r['customers']} clientes, {r['msgs_in']} in / {r['msgs_out']} out")
        total_in += r["msgs_in"]
        total_out += r["msgs_out"]

    # Order stats
    order_row = await db.fetch_one(
        """
        SELECT COUNT(*) as total,
               COUNT(*) FILTER (WHERE payment_status = 'pending') as pending,
               COUNT(*) FILTER (WHERE payment_status = 'proof_received') as proof,
               COUNT(*) FILTER (WHERE payment_status = 'confirmed') as confirmed,
               COALESCE(SUM(total), 0) as revenue
        FROM orders WHERE created_at >= CURRENT_DATE
        """
    )

    # Escalation count
    esc_row = await db.fetch_one(
        "SELECT COUNT(*) as cnt FROM customers WHERE conversation_state = 'escalated'"
    )

    o = dict(order_row) if order_row else {}
    esc = esc_row["cnt"] if esc_row else 0

    return (
        f"📊 *Estadísticas de hoy*\n\n"
        f"*Mensajes:* {total_in} recibidos / {total_out} enviados\n"
        + ("\n".join(conv_lines) + "\n\n" if conv_lines else "  Sin mensajes aún\n\n")
        + f"*Pedidos:* {o.get('total', 0)}\n"
        f"  Pendientes: {o.get('pending', 0)}\n"
        f"  Con comprobante: {o.get('proof', 0)}\n"
        f"  Confirmados: {o.get('confirmed', 0)}\n"
        f"  Ingresos: ${float(o.get('revenue', 0)):.2f}\n\n"
        f"⚠️ *Escalaciones activas:* {esc}"
    )


async def _cmd_customers(args: str) -> str:
    tag_filter = args.strip() if args.strip() else None

    if tag_filter:
        rows = await db.fetch_all(
            """
            SELECT id, display_name, channel, platform_id, tags, total_orders, last_active
            FROM customers
            WHERE tags @> CAST(:tag AS jsonb)
            ORDER BY last_active DESC NULLS LAST LIMIT 10
            """,
            {"tag": json.dumps([tag_filter])},
        )
        header = f"👥 *Clientes con tag '{escape_markdown(tag_filter)}':*\n\n"
    else:
        rows = await db.fetch_all(
            """
            SELECT id, display_name, channel, platform_id, tags, total_orders, last_active
            FROM customers
            ORDER BY last_active DESC NULLS LAST LIMIT 10
            """
        )
        header = "👥 *Clientes recientes:*\n\n"

    if not rows:
        return header + "No se encontraron clientes."

    lines = []
    for r in rows:
        name = escape_markdown(r["display_name"] or r["platform_id"][:12])
        short_id = escape_markdown(str(r["id"])[:8])
        tags = r["tags"] if isinstance(r["tags"], list) else json.loads(r["tags"] or "[]")
        tag_str = escape_markdown(", ".join(tags[:4]))
        lines.append(f"• `{short_id}` *{name}* ({escape_markdown(r['channel'])})\n  Pedidos: {r['total_orders']} | Tags: {tag_str}")

    return header + "\n".join(lines)


async def _cmd_orders() -> str:
    rows = await db.fetch_all(
        """
        SELECT o.id, c.display_name, o.total, o.payment_method,
               o.payment_status, o.shipping_status, o.created_at
        FROM orders o
        LEFT JOIN customers c ON o.customer_id = c.id
        ORDER BY COALESCE(c.display_name, 'zzz') ASC, o.created_at DESC LIMIT 10
        """
    )

    if not rows:
        return "📦 *Pedidos recientes:* Ninguno"

    lines = ["📦 *Pedidos recientes:*\n"]
    for r in rows:
        name = escape_markdown(r["display_name"] or "Desconocido")
        short_id = escape_markdown(str(r["id"])[:8])
        lines.append(
            f"• `{short_id}` - *{name}*\n"
            f"  ${float(r['total']):.2f} via {escape_markdown(r['payment_method'] or '')}\n"
            f"  Pago: {escape_markdown(r['payment_status'] or '')} | Envío: {escape_markdown(r['shipping_status'] or '')}"
        )

    return "\n".join(lines)


_VALID_ORDER_STATUSES = orders.VALID_PAYMENT_STATUSES | orders.VALID_SHIPPING_STATUSES


async def _cmd_update_order(args: str) -> str:
    parts = args.strip().split()
    if len(parts) < 2:
        options = ", ".join(sorted(_VALID_ORDER_STATUSES))
        return (
            "Uso: /order ID STATUS\n"
            "Ejemplo: /order abc123 confirmed\n"
            f"Estados: {options}"
        )

    order_id_prefix = parts[0]
    new_status = parts[1].lower()

    if new_status not in _VALID_ORDER_STATUSES:
        return f"Estado inválido. Opciones: {', '.join(sorted(_VALID_ORDER_STATUSES))}"

    # Find order by ID prefix
    row = await db.fetch_one(
        "SELECT id FROM orders WHERE id::text LIKE :prefix",
        {"prefix": f"{order_id_prefix}%"},
    )

    if not row:
        return f"No se encontró un pedido con ID que empiece con '{escape_markdown(order_id_prefix)}'"

    order_id = str(row["id"])

    # Route to the authoritative order service based on the status type
    if new_status in orders.VALID_PAYMENT_STATUSES:
        await orders.update_order_payment_status(order_id, status=new_status)
    elif new_status in orders.VALID_SHIPPING_STATUSES:
        await orders.update_order_shipping(order_id, shipping_status=new_status)

    return f"✅ Pedido `{escape_markdown(order_id[:8])}` actualizado a *{escape_markdown(new_status)}*"


async def _cmd_resolve(args: str) -> str:
    arg = args.strip().lower()
    if not arg:
        # Show all escalated customers
        rows = await db.fetch_all(
            "SELECT id, display_name, channel, platform_id FROM customers WHERE conversation_state = 'escalated'"
        )
        if not rows:
            return "✅ No hay clientes escalados."
        lines = ["🔴 *Clientes escalados:*\n"]
        for r in rows:
            name = escape_markdown(r["display_name"] or r["platform_id"])
            lines.append(f"• `{escape_markdown(str(r['id'])[:8])}` {name} ({escape_markdown(r['channel'])})")
        lines.append("\nUsa /resolve ID o /resolve all")
        return "\n".join(lines)

    if arg == "all":
        rows = await db.fetch_all(
            "SELECT id FROM customers WHERE conversation_state = 'escalated'"
        )
        count = len(rows)
        if count == 0:
            return "✅ No hay clientes escalados."
        resolved = 0
        failed = 0
        for row in rows:
            try:
                await activate_customer_for_admin(dict(row))
                resolved += 1
            except ManualActivationError:
                failed += 1
        if failed:
            return f"⚠️ {resolved} cliente(s) resueltos y {failed} fallaron. Revisa la configuración de Kommo si aplica."
        return f"✅ {resolved} cliente(s) resueltos. El bot volverá a atenderles con un chat nuevo."

    row = await db.fetch_one(
        "SELECT id, display_name FROM customers WHERE id::text LIKE :prefix AND conversation_state = 'escalated'",
        {"prefix": f"{arg}%"},
    )

    if not row:
        return f"No se encontró cliente escalado con ID que empiece con '{escape_markdown(arg)}'"

    try:
        await activate_customer_for_admin(dict(row))
    except ManualActivationError as e:
        return f"⚠️ No pude reactivar este cliente: {e.safe_detail}"
    name = escape_markdown(row["display_name"] or str(row["id"])[:8])
    return f"✅ Escalación resuelta para *{name}*. El bot volverá a atenderle con un chat nuevo."


async def _cmd_switch_provider(args: str) -> str:
    config = get_config()
    if config.llm_managed_externally:
        return "⚙️ El proveedor de AI es administrado por el Master Admin. Contacta a tu administrador para cambios."

    provider = args.strip().lower()
    if provider not in ("openai", "anthropic"):
        return "Uso: /provider openai o /provider anthropic"

    default_model = next(
        (m["id"] for m in AVAILABLE_MODELS.get(provider, []) if m.get("default")),
        AVAILABLE_MODELS[provider][0]["id"],
    )

    await db.execute(
        "UPDATE settings SET value = :val, updated_at = NOW() WHERE key = 'llm_provider'",
        {"val": json.dumps(provider)},
    )
    await db.execute(
        "UPDATE settings SET value = :val, updated_at = NOW() WHERE key = 'llm_model'",
        {"val": json.dumps(default_model)},
    )
    db.invalidate_settings_cache()

    return f"⚙️ Proveedor cambiado a *{provider}* / {default_model}"


async def _cmd_list_broadcasts() -> str:
    broadcasts = await list_broadcasts(limit=10)

    if not broadcasts:
        return "📢 *Broadcasts:* Ninguno creado"

    lines = ["📢 *Broadcasts recientes:*\n"]
    for b in broadcasts:
        short_id = escape_markdown(str(b["id"])[:8])
        tags = b["target_tags"] if isinstance(b["target_tags"], list) else json.loads(b["target_tags"] or "[]")
        lines.append(
            f"• `{short_id}` - *{escape_markdown(b['name'])}*\n"
            f"  Plantilla: {escape_markdown(b['template_name'])}\n"
            f"  Tags: {escape_markdown(', '.join(tags))}\n"
            f"  Estado: {escape_markdown(b['status'])} | Enviados: {b.get('recipients', 0)}"
        )

    return "\n".join(lines)


async def _cmd_send_broadcast(args: str) -> str:
    broadcast_id_prefix = args.strip()
    if not broadcast_id_prefix:
        return "Uso: /send BROADCAST_ID_PREFIX"

    row = await db.fetch_one(
        "SELECT id, name FROM broadcasts WHERE id::text LIKE :prefix AND status IN ('draft', 'scheduled')",
        {"prefix": f"{broadcast_id_prefix}%"},
    )

    if not row:
        return f"No se encontró broadcast pendiente con ID que empiece con '{escape_markdown(broadcast_id_prefix)}'"

    result = await execute_broadcast(str(row["id"]))
    return f"📢 Broadcast *{escape_markdown(row['name'])}* ejecutado: {result.get('recipients', 0)} enviados, {result.get('errors', 0)} errores"


async def _cmd_preview(args: str) -> str:
    tags = [t.strip() for t in args.split(",") if t.strip()]
    if not tags:
        return "Uso: /preview tag1,tag2\nEjemplo: /preview vip,interested:pajamas"

    result = await preview_broadcast(tags)
    return (
        f"📋 *Vista previa de broadcast*\n\n"
        f"Tags: {escape_markdown(', '.join(tags))}\n"
        f"Clientes que coinciden: *{result['matching_customers']}*\n"
        f"Costo estimado: *${result['estimated_cost_usd']:.2f}*"
    )


async def _cmd_settings() -> str:
    settings = await db.get_settings()
    config = get_config()

    ai_status = "activado ✅" if bool(settings.get("ai_enabled", True)) else "pausado ⏸️"
    orchestration = settings.get("ai_orchestration_mode", "legacy")
    provider = settings.get("llm_provider", "")
    model = settings.get("llm_model", "")

    lines = [
        "⚙️ *Configuración actual:*\n",
        f"🤖 *AI:* {escape_markdown(ai_status)}",
        f"• Orquestación: {escape_markdown(orchestration)}",
        f"• Proveedor: {escape_markdown(provider)} / {escape_markdown(model)}",
    ]

    fallback_provider = settings.get("fallback_provider", "")
    if fallback_provider:
        fallback_model = settings.get("fallback_model", "")
        auto_fallback = bool(settings.get("auto_fallback", True))
        lines.append(
            f"• Fallback: {escape_markdown(fallback_provider)} / {escape_markdown(fallback_model)} "
            f"(auto: {'sí' if auto_fallback else 'no'})"
        )

    selected = selected_exchange_rate(settings)
    rate = selected["rate"]
    rate_text = format_rate_for_customer(rate) if rate is not None else "no disponible"
    lines.extend([
        "\n💱 *Tasa de cambio*",
        f"• Referencia: {escape_markdown(selected['reference'].label)}",
        f"• Valor: {rate_text} Bs por {escape_markdown(selected['reference'].unit)}",
    ])

    commerce_lines = []
    store_phone = settings.get("store_phone_number")
    if store_phone:
        commerce_lines.append(f"• Teléfono de la tienda: {escape_markdown(store_phone)}")
    discount_percent = settings.get("order_discount_percent") or 0
    discount_threshold = settings.get("order_discount_threshold_usd") or 0
    try:
        if float(discount_percent) > 0 and float(discount_threshold) > 0:
            commerce_lines.append(
                f"• Descuento: {discount_percent}% en pedidos > ${float(discount_threshold):.2f}"
            )
    except (TypeError, ValueError):
        pass
    if commerce_lines:
        lines.append("\n🤝 *Comercio*")
        lines.extend(commerce_lines)

    escalation_telegram = bool(settings.get("escalation_telegram_enabled", True))
    lines.extend([
        "\n🔔 *Telegram*",
        f"• Escalaciones por Telegram: {'activadas' if escalation_telegram else 'desactivadas'}",
    ])

    backend = getattr(config, "channel_backend", "meta")
    lines.append("\n⚙️ *Canal*")
    if backend == "kommo":
        kommo_notes = []
        if getattr(config, "kommo_chats_media_enabled", False):
            kommo_notes.append("media en chats")
        if getattr(config, "kommo_chats_product_images_enabled", False):
            kommo_notes.append("imágenes de productos")
        if getattr(config, "kommo_chats_catalog_pdf_enabled", False):
            kommo_notes.append("PDF de catálogo")
        suffix = f" ({', '.join(kommo_notes)})" if kommo_notes else ""
        lines.append(f"• Backend: kommo{escape_markdown(suffix)}")
    else:
        lines.append("• Backend: meta")

    return "\n".join(lines)


async def _cmd_usage() -> str:
    rows = await db.fetch_all(
        """
        SELECT provider, model, COUNT(*) as calls,
               SUM(input_tokens) as inp, SUM(output_tokens) as out
        FROM usage_log WHERE created_at >= CURRENT_DATE
        GROUP BY provider, model
        """
    )

    if not rows:
        return "💰 *Uso de hoy:* Sin llamadas a LLM"

    lines = ["💰 *Uso de tokens hoy:*\n"]
    total = 0.0

    for r in rows:
        rates = get_model_costs(r["model"])
        cost = (r["inp"] / 1e6) * rates["input"] + (r["out"] / 1e6) * rates["output"]
        total += cost
        lines.append(
            f"  *{r['provider']}/{r['model']}*\n"
            f"    Llamadas: {r['calls']} | Tokens: {r['inp']}in / {r['out']}out\n"
            f"    Costo: ${cost:.4f}"
        )

    lines.append(f"\n*Total estimado:* ${total:.4f}")
    return "\n".join(lines)


async def _cmd_conversion(args: str) -> str:
    days = int(args.strip()) if args.strip().isdigit() else 7
    data = await get_conversion_funnel(days=days)

    f = data["funnel"]
    r = data["rates"]

    return (
        f"📊 *Embudo de conversión ({days} días)*\n\n"
        f"1. Mensajaron: *{f['messaged']}*\n"
        f"2. Preguntaron por productos: *{f['inquired_products']}* ({r['inquiry_rate']})\n"
        f"3. Hicieron pedido: *{f['placed_order']}* ({r['order_rate']})\n"
        f"4. Pagaron: *{f['paid']}* ({r['payment_rate']})\n\n"
        f"Conversión total: *{r['overall_conversion']}*"
    )


async def _cmd_performance(args: str) -> str:
    days = int(args.strip()) if args.strip().isdigit() else 7
    data = await get_response_time_stats(days=days)

    if not data["providers"]:
        return "📊 *Tiempos de respuesta:* Sin datos aún"

    lines = [f"📊 *Tiempos de respuesta ({days} días)*\n"]
    for p in data["providers"]:
        lines.append(
            f"  *{p['provider']}/{p['model']}*\n"
            f"    Avg: {p['avg_ms']}ms | P50: {p['p50_ms']}ms | P95: {p['p95_ms']}ms\n"
            f"    Min: {p['min_ms']}ms | Max: {p['max_ms']}ms | Calls: {p['calls']}"
        )
    return "\n".join(lines)


async def _cmd_ai_toggle(args: str) -> str:
    mode = args.strip().lower()
    if mode not in ("on", "off"):
        # Show current state
        settings = await db.get_settings()
        enabled = settings.get("ai_enabled", True)
        status = "activado ✅" if enabled else "pausado ⏸️"
        return (
            f"🤖 *Estado del AI:* {status}\n\n"
            f"Usa /ai on o /ai off para cambiar."
        )

    enabled = mode == "on"
    await db.execute(
        """
        INSERT INTO settings (key, value) VALUES ('ai_enabled', :val)
        ON CONFLICT (key) DO UPDATE SET value = :val, updated_at = NOW()
        """,
        {"val": json.dumps(enabled)},
    )
    db.invalidate_settings_cache()

    if enabled:
        return (
            "🤖 *AI activado* ✅\n\n"
            "El chatbot responderá automáticamente a los clientes."
        )
    return (
        "⏸️ *AI pausado*\n\n"
        "El chatbot NO responderá. Los mensajes entrantes "
        "se te reenviarán aquí para que respondas manualmente.\n"
        "Usa /ai on para reactivar."
    )


async def _cmd_generate_catalog_pdf() -> str:
    catalog = get_cached_catalog()
    if not catalog:
        return "❌ El catálogo está vacío. Verifica la conexión a Google Sheets."

    try:
        generate_catalog_pdf(catalog)
        meta = get_pdf_metadata()
        config = get_config()
        pdf_url = f"{config.app_base_url}/static/catalog/catalog.pdf"
        return (
            f"✅ *PDF del catálogo generado*\n\n"
            f"Productos: {meta.get('product_count', 0)}\n"
            f"URL: {pdf_url}\n"
            f"Descarga: {config.app_base_url}/admin/settings/catalog/download-pdf"
        )
    except Exception as e:
        return f"❌ Error generando el PDF: {e}"


async def _cmd_popular_products() -> str:
    products = await get_popular_products(days=30)

    if not products:
        return "📦 *Productos populares:* Sin datos aún"

    lines = ["📦 *Productos más buscados (30 días)*\n"]
    for i, p in enumerate(products[:10], 1):
        size_note = f" (talla {escape_markdown(p['size_filter'])})" if p.get("size_filter") else ""
        lines.append(f"  {i}. *{escape_markdown(p['query'])}*{size_note} - {p['times_asked']} consultas")

    return "\n".join(lines)


async def _cmd_tags(args: str) -> str:
    """Show all tags for a customer. Usage: /tags CUSTOMER_ID_PREFIX"""
    prefix = args.strip()
    if not prefix:
        return "Uso: /tags ID\nMuestra los tags de un cliente."

    row = await db.fetch_one(
        "SELECT id, display_name, platform_id, tags FROM customers WHERE id::text LIKE :prefix",
        {"prefix": f"{prefix}%"},
    )
    if not row:
        return f"No se encontró cliente con ID que empiece con '{escape_markdown(prefix)}'"

    name = escape_markdown(row["display_name"] or row["platform_id"])
    tags = row["tags"] if isinstance(row["tags"], list) else json.loads(row["tags"] or "[]")

    if not tags:
        return f"🏷️ *{name}* no tiene tags."

    tag_list = "\n".join(f"• `{escape_markdown(t)}`" for t in tags)
    return f"🏷️ *Tags de {name}:*\n\n{tag_list}\n\nUsa /tag ID add tag1,tag2 o /tag ID del tag1"


async def _cmd_tag_edit(args: str) -> str:
    """Add or remove tags. Usage: /tag ID add tag1,tag2  or  /tag ID del tag1"""
    parts = args.strip().split(None, 2)
    if len(parts) < 3:
        return (
            "Uso:\n"
            "/tag ID add tag1,tag2 - Agregar tags\n"
            "/tag ID del tag1 - Eliminar un tag"
        )

    prefix, action, tag_str = parts
    action = action.lower()

    row = await db.fetch_one(
        "SELECT id, display_name, platform_id FROM customers WHERE id::text LIKE :prefix",
        {"prefix": f"{prefix}%"},
    )
    if not row:
        return f"No se encontró cliente con ID que empiece con '{escape_markdown(prefix)}'"

    name = escape_markdown(row["display_name"] or row["platform_id"])
    cid = str(row["id"])

    if action == "add":
        new_tags = [t.strip() for t in tag_str.split(",") if t.strip()]
        if not new_tags:
            return "Especifica al menos un tag."
        await add_tags(cid, new_tags)
        return f"✅ Tags agregados a *{name}*: {escape_markdown(', '.join(new_tags))}"

    elif action in ("del", "remove", "rm"):
        tag = tag_str.strip()
        if not tag:
            return "Especifica el tag a eliminar."
        await remove_tag(cid, tag)
        return f"✅ Tag `{escape_markdown(tag)}` eliminado de *{name}*"

    else:
        return "Acción no reconocida. Usa `add` o `del`."


# -- Telegram webhook setup helper ------------------------------------

async def setup_telegram_webhook(bot_token: str, webhook_url: str, webhook_secret: str):
    """
    Register the Telegram webhook URL with the Telegram Bot API.
    Call this once after deployment.
    """
    install_secret_redaction_filter()
    url = f"https://api.telegram.org/bot{bot_token}/setWebhook"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            json={"url": webhook_url, "secret_token": webhook_secret},
        )
        logger.info(f"Telegram webhook setup: {resp.json()}")
        return resp.json()
