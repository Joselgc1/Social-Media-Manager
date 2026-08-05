"""
Send notifications to the store owner via Telegram.
Used for human escalation and important alerts.
"""

import logging

from app.admin.telegram_sender import escape_markdown, send_message

logger = logging.getLogger(__name__)


async def notify_owner(message: str):
    """
    Send a plain-text message to the admin's Telegram chat.
    Silently skips if Telegram is not configured or unreachable.
    Delegates to the central safe sender for chunking and status checking.
    """
    await send_message(message)


async def notify_escalation(
    customer_name: str | None,
    customer_channel: str,
    customer_platform_id: str,
    reason: str,
    urgency: str,
    conversation_summary: str,
):
    """
    Send a structured escalation alert to the store owner.
    Includes customer info, reason, and recent conversation.
    Customer-provided content is escaped before composing.
    """
    urgency_emoji = {"low": "🟡", "medium": "🟠", "high": "🔴"}.get(urgency, "⚪")

    # Build a direct link to the customer
    if customer_channel == "whatsapp":
        direct_link = f"https://wa.me/{escape_markdown(customer_platform_id)}"
    else:
        direct_link = f"https://ig.me/m/{escape_markdown(customer_platform_id)}"

    message = (
        f"{urgency_emoji} *ESCALACIÓN - {urgency.upper()}*\n\n"
        f"*Cliente:* {escape_markdown(customer_name or 'Desconocido')}\n"
        f"*Canal:* {escape_markdown(customer_channel)}\n"
        f"*Razón:* {escape_markdown(reason)}\n\n"
        f"*Últimos mensajes:*\n{escape_markdown(conversation_summary)}\n\n"
        f"[Responder directamente]({direct_link})"
    )

    await notify_owner(message)


async def notify_incoming_message(
    customer_name: str | None,
    customer_channel: str,
    customer_platform_id: str,
    message_text: str,
):
    """
    Forward an incoming customer message to the owner via Telegram.
    Used when AI is paused (globally or per-customer).
    """
    if customer_channel == "whatsapp":
        direct_link = f"https://wa.me/{escape_markdown(customer_platform_id)}"
    else:
        direct_link = f"https://ig.me/m/{escape_markdown(customer_platform_id)}"

    message = (
        f"📩 *Mensaje recibido* (AI pausado)\n\n"
        f"*De:* {escape_markdown(customer_name or customer_platform_id)}\n"
        f"*Canal:* {escape_markdown(customer_channel)}\n"
        f"*Mensaje:* {escape_markdown(message_text[:500])}\n\n"
        f"[Responder]({direct_link})"
    )

    await notify_owner(message)


async def notify_new_order(
    customer_name: str | None,
    order_total: float,
    payment_method: str,
    items_summary: str,
    delivery_summary: str | None = None,
):
    """Notify the owner when a new order is created."""
    delivery_line = f"\n*Entrega:* {escape_markdown(delivery_summary)}" if delivery_summary else ""
    message = (
        f"🛒 *NUEVA ORDEN*\n\n"
        f"*Cliente:* {escape_markdown(customer_name or 'Desconocido')}\n"
        f"*Total:* ${order_total:.2f}\n"
        f"*Pago:* {escape_markdown(payment_method)}\n"
        f"*Productos:* {escape_markdown(items_summary)}"
        f"{delivery_line}"
    )

    await notify_owner(message)
