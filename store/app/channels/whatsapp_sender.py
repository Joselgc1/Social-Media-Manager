"""
WhatsApp Cloud API sender.
Handles sending text messages, images, interactive buttons, and template messages.
"""

import logging

import httpx

from app.channels.meta_errors import MetaSendError
from app.channels.text_formatting import format_customer_text
from app.config import get_config

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.facebook.com/v21.0"


async def send_text(to: str, text: str):
    """Send a plain text message to a WhatsApp number."""
    config = get_config()
    url = f"{GRAPH_API}/{config.whatsapp_phone_number_id}/messages"
    body_text = format_customer_text(text, "whatsapp")

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body_text},
    }

    return await _send(url, payload, config.whatsapp_access_token)


async def send_image(to: str, image_url: str, caption: str = ""):
    """Send an image to a WhatsApp number via a public URL."""
    config = get_config()
    url = f"{GRAPH_API}/{config.whatsapp_phone_number_id}/messages"

    image_payload: dict = {"link": image_url}
    if caption:
        image_payload["caption"] = format_customer_text(caption, "whatsapp")

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": image_payload,
    }

    return await _send(url, payload, config.whatsapp_access_token)


async def send_interactive_buttons(to: str, body_text: str, buttons: list[str]):
    """
    Send a message with up to 3 reply buttons.
    Each button label must be <= 20 characters.
    """
    config = get_config()
    url = f"{GRAPH_API}/{config.whatsapp_phone_number_id}/messages"

    button_objects = [
        {
            "type": "reply",
            "reply": {
                "id": f"btn_{i}",
                "title": label[:20],  # Enforce 20-char limit
            },
        }
        for i, label in enumerate(buttons[:3])  # Max 3 buttons
    ]

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": format_customer_text(body_text, "whatsapp")},
            "action": {"buttons": button_objects},
        },
    }

    return await _send(url, payload, config.whatsapp_access_token)


async def send_template(to: str, template_name: str, language: str = "es", parameters: list[str] | None = None):
    """
    Send a pre-approved template message (used for broadcasts).
    Parameters are dynamic variables injected into the template body.
    """
    config = get_config()
    url = f"{GRAPH_API}/{config.whatsapp_phone_number_id}/messages"

    components = []
    if parameters:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": p} for p in parameters],
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": components,
        },
    }

    return await _send(url, payload, config.whatsapp_access_token)


async def send_document(to: str, document_url: str, filename: str = "catalogo.pdf", caption: str = ""):
    """
    Send a document (PDF) to a WhatsApp number via a public URL.
    The URL must be publicly accessible by Meta's servers.
    """
    config = get_config()
    url = f"{GRAPH_API}/{config.whatsapp_phone_number_id}/messages"

    doc: dict = {"link": document_url, "filename": filename}
    if caption:
        doc["caption"] = format_customer_text(caption, "whatsapp")

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": doc,
    }

    return await _send(url, payload, config.whatsapp_access_token)


async def mark_as_read(message_id: str):
    """Mark a received message as read (shows blue checkmarks)."""
    config = get_config()
    url = f"{GRAPH_API}/{config.whatsapp_phone_number_id}/messages"

    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }

    return await _send(url, payload, config.whatsapp_access_token)


# ── Internal helper ──────────────────────────────────────────

async def _send(url: str, payload: dict, access_token: str):
    """POST to the WhatsApp Cloud API and raise on delivery failure."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        raise MetaSendError("WhatsApp API connection failed before sending", retryable=True) from exc

    if not resp.is_success:
        logger.error(f"WhatsApp API error ({resp.status_code})")
        raise MetaSendError(
            f"WhatsApp API error {resp.status_code}: {resp.text}",
            retryable=resp.status_code == 429 or resp.status_code >= 500 or resp.status_code in {401, 403},
        )

    response_json = resp.json()
    logger.debug(f"WhatsApp message sent: {response_json}")
    return response_json
