"""
Instagram Graph API message sender.
Handles sending text messages, images, quick replies, and ice breakers
through the Instagram Messaging API.

Key differences from WhatsApp:
- Uses Page Access Token (not a separate WhatsApp token)
- Endpoint is /me/messages on graph.facebook.com
- Supports Quick Replies (up to 13 options) instead of interactive buttons
- No template messages (no outbound broadcasts on Instagram)
- 24-hour messaging window: can only reply within 24h of customer's last message
- 200 automated DMs per hour rate limit
"""

import logging
import httpx
from app.config import get_config

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.facebook.com/v21.0"


async def send_text(to: str, text: str):
    """
    Send a plain text DM to an Instagram user.

    Parameters
    ----------
    to : str
        Instagram-scoped user ID (NOT the username).
    text : str
        Message body (max 1000 bytes UTF-8).
    """
    config = get_config()
    url = f"{GRAPH_API}/me/messages"

    payload = {
        "recipient": {"id": to},
        "message": {"text": text[:1000]},  # Enforce 1000-byte limit
    }

    await _send(url, payload, config.instagram_access_token)


async def send_text_with_quick_replies(to: str, text: str, quick_replies: list[dict]):
    """
    Send a text message with Quick Reply buttons.
    Quick Replies appear as tappable pills below the message (mobile only).

    Parameters
    ----------
    quick_replies : list[dict]
        Each dict: {"title": "Display text", "payload": "CALLBACK_PAYLOAD"}
        Max 13 options, each title max 20 characters.
    """
    config = get_config()
    url = f"{GRAPH_API}/me/messages"

    qr_objects = [
        {
            "content_type": "text",
            "title": qr["title"][:20],
            "payload": qr.get("payload", qr["title"].upper().replace(" ", "_")),
        }
        for qr in quick_replies[:13]
    ]

    payload = {
        "recipient": {"id": to},
        "message": {
            "text": text[:1000],
            "quick_replies": qr_objects,
        },
    }

    await _send(url, payload, config.instagram_access_token)


async def send_image(to: str, image_url: str):
    """Send an image message (e.g., product photo)."""
    config = get_config()
    url = f"{GRAPH_API}/me/messages"

    payload = {
        "recipient": {"id": to},
        "message": {
            "attachment": {
                "type": "image",
                "payload": {"url": image_url},
            }
        },
    }

    await _send(url, payload, config.instagram_access_token)


async def send_generic_template(to: str, elements: list[dict]):
    """
    Send a generic template (carousel of cards with images, titles, and buttons).
    Useful for showing multiple products.

    Parameters
    ----------
    elements : list[dict]
        Each dict: {
            "title": "Product name",
            "subtitle": "$28.00 - Tallas: S, M, L",
            "image_url": "https://...",
            "buttons": [{"type": "postback", "title": "Ver detalles", "payload": "PRODUCT_SKU"}]
        }
        Max 10 elements.
    """
    config = get_config()
    url = f"{GRAPH_API}/me/messages"

    payload = {
        "recipient": {"id": to},
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "generic",
                    "elements": elements[:10],
                },
            }
        },
    }

    await _send(url, payload, config.instagram_access_token)


async def send_private_reply(comment_id: str, text: str):
    """
    Send a single private reply to an Instagram comment.
    This does NOT open a full conversation window; the customer
    must reply to your DM to start a conversation.
    """
    config = get_config()
    url = f"{GRAPH_API}/me/messages"

    payload = {
        "recipient": {"comment_id": comment_id},
        "message": {"text": text[:1000]},
    }

    await _send(url, payload, config.instagram_access_token)


# ── Ice Breakers ─────────────────────────────────────────────

async def setup_ice_breakers(ig_user_id: str, ice_breakers: list[dict] | None = None):
    """
    Configure Ice Breaker prompts that appear when a customer opens
    your DM for the first time. Max 4 options.

    Parameters
    ----------
    ig_user_id : str
        Your Instagram Professional account's user ID.
    ice_breakers : list[dict]
        Each dict: {"question": "Display text", "payload": "CALLBACK_PAYLOAD"}
        If None, uses default Spanish prompts for this business.
    """
    config = get_config()

    if ice_breakers is None:
        ice_breakers = [
            {"question": "¿Qué productos tienen?", "payload": "BROWSE_PRODUCTS"},
            {"question": "¿Cuáles son los precios?", "payload": "PRICING"},
            {"question": "¿Cómo puedo pagar?", "payload": "PAYMENT_INFO"},
            {"question": "¿Hacen envíos a mi ciudad?", "payload": "SHIPPING_INFO"},
        ]

    url = f"{GRAPH_API}/{ig_user_id}/ice_breakers"
    payload = {"ice_breakers": ice_breakers[:4]}

    await _send(url, payload, config.instagram_access_token)
    logger.info(f"Ice Breakers configured: {[ib['question'] for ib in ice_breakers]}")


async def delete_ice_breakers(ig_user_id: str):
    """Remove all Ice Breakers from the Instagram account."""
    config = get_config()
    url = f"{GRAPH_API}/{ig_user_id}/ice_breakers"

    headers = {"Authorization": f"Bearer {config.instagram_access_token}"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(url, headers=headers)
            if resp.status_code != 200:
                logger.error(f"Failed to delete Ice Breakers: {resp.text}")
    except Exception as e:
        logger.error(f"Failed to delete Ice Breakers: {e}")


# ── Page subscription ────────────────────────────────────────

async def subscribe_page_to_webhooks(page_id: str):
    """
    Enable webhook subscriptions for the Facebook Page linked to Instagram.
    Must be called once after app setup to start receiving DM notifications.
    """
    config = get_config()
    url = f"{GRAPH_API}/{page_id}/subscribed_apps"

    payload = {
        "subscribed_fields": "messages,messaging_postbacks",
    }

    await _send(url, payload, config.instagram_access_token)
    logger.info(f"Page {page_id} subscribed to messaging webhooks.")


# ── Internal helper ──────────────────────────────────────────

async def _send(url: str, payload: dict, access_token: str):
    """POST to the Instagram/Graph API with error logging."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload, headers=headers)

            if resp.status_code != 200:
                error_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text
                logger.error(f"Instagram API error ({resp.status_code}): {error_data}")
            else:
                logger.debug(f"Instagram message sent: {resp.json()}")

    except Exception as e:
        logger.error(f"Failed to send Instagram message: {e}")
