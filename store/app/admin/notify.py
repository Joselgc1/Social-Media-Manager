"""
Send notifications to the store owner via Telegram.
Used for human escalation and important alerts.
"""

import logging

import httpx

from app.config import get_config
from app.log_redaction import install_secret_redaction_filter

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


async def notify_owner(message: str):
    """
    Send a plain-text message to the admin's Telegram chat.
    Silently skips if Telegram is not configured or unreachable.
    """
    config = get_config()
    install_secret_redaction_filter()

    if not config.telegram_bot_token or not config.telegram_admin_chat_id:
        logger.debug("Telegram not configured, skipping notification.")
        return

    url = TELEGRAM_API.format(token=config.telegram_bot_token)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={
                "chat_id": config.telegram_admin_chat_id,
                "text": message,
                "parse_mode": "Markdown",
            })
    except Exception as e:
        logger.error(f"Failed to send Telegram notification: {e}")


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
    """
    urgency_emoji = {"low": "🟡", "medium": "🟠", "high": "🔴"}.get(urgency, "⚪")

    # Build a direct link to the customer
    if customer_channel == "whatsapp":
        direct_link = f"https://wa.me/{customer_platform_id}"
    else:
        direct_link = f"https://ig.me/m/{customer_platform_id}"

    message = (
        f"{urgency_emoji} *ESCALACIÓN - {urgency.upper()}*\n\n"
        f"*Cliente:* {customer_name or 'Desconocido'}\n"
        f"*Canal:* {customer_channel}\n"
        f"*Razón:* {reason}\n\n"
        f"*Últimos mensajes:*\n{conversation_summary}\n\n"
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
        direct_link = f"https://wa.me/{customer_platform_id}"
    else:
        direct_link = f"https://ig.me/m/{customer_platform_id}"

    message = (
        f"📩 *Mensaje recibido* (AI pausado)\n\n"
        f"*De:* {customer_name or customer_platform_id}\n"
        f"*Canal:* {customer_channel}\n"
        f"*Mensaje:* {message_text[:500]}\n\n"
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
    delivery_line = f"\n*Entrega:* {delivery_summary}" if delivery_summary else ""
    message = (
        f"🛒 *NUEVA ORDEN*\n\n"
        f"*Cliente:* {customer_name or 'Desconocido'}\n"
        f"*Total:* ${order_total:.2f}\n"
        f"*Pago:* {payment_method}\n"
        f"*Productos:* {items_summary}"
        f"{delivery_line}"
    )

    await notify_owner(message)
