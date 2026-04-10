"""
Instagram DM webhook handler.
Receives incoming messages from the Instagram Messaging API (via Meta Graph API),
normalizes them, and routes them through the AI engine.

Instagram message types handled:
- text: Plain text DMs
- attachments: Images, audio, video, files, shares, story_mention
- postback: Ice Breaker taps and Quick Reply callbacks
- story_reply: Replies to Instagram Stories
- referral: Clicks from ads or links that open the DM
- message_delete: Customer deleted a message (acknowledged, no action)

Key constraints:
- 24-hour messaging window (can only reply within 24h of customer's last message)
- 200 automated DMs per hour
- Quick Replies and Ice Breakers only work on mobile
- 1000-byte UTF-8 message text limit
"""

import hashlib
import hmac
import logging
from fastapi import APIRouter, Request, Response, HTTPException

from app.config import get_config
from app.ai.engine import generate_response
from app.channels.instagram_sender import (
    send_image,
    send_text,
    send_text_with_quick_replies,
)
from app.admin.notify import notify_owner
from app.webhooks.inbound_buffer import enqueue_inbound_message

logger = logging.getLogger(__name__)
router = APIRouter()


def _mask_sender(sender: str) -> str:
    if len(sender) <= 4:
        return sender
    return f"{sender[:2]}***{sender[-2:]}"


async def _safe_send_apology(sender_id: str):
    try:
        await send_text(
            to=sender_id,
            text="Disculpa, tuve un problema procesando tu mensaje. ¿Puedes intentar de nuevo? 🙏",
        )
    except Exception as send_error:
        logger.error(f"Failed to send Instagram fallback reply to {_mask_sender(sender_id)}: {send_error}")


async def _notify_delivery_failure(sender_id: str, detail: str):
    await notify_owner(
        f"⚠️ *Fallo enviando respuesta por Instagram*\n\n"
        f"*Cliente:* `{_mask_sender(sender_id)}`\n"
        f"*Detalle:* {detail[:400]}"
    )

# Map Ice Breaker payloads to context hints for the AI
ICE_BREAKER_CONTEXT = {
    "BROWSE_PRODUCTS": "El cliente quiere ver los productos disponibles.",
    "PRICING": "El cliente quiere saber los precios.",
    "PAYMENT_INFO": "El cliente quiere saber los métodos de pago aceptados.",
    "SHIPPING_INFO": "El cliente quiere saber sobre los envíos y cobertura.",
}


# ── Webhook verification (GET) ───────────────────────────────

@router.get("/webhooks/instagram")
async def verify_instagram(request: Request):
    """
    Meta sends a GET request to verify your webhook URL.
    Respond with the hub.challenge value.
    """
    config = get_config()
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == config.instagram_verify_token:
        logger.info("Instagram webhook verified successfully.")
        return Response(content=challenge, status_code=200)

    logger.warning("Instagram webhook verification failed.")
    return Response(status_code=403)


# ── Incoming messages (POST) ─────────────────────────────────

@router.post("/webhooks/instagram")
async def handle_instagram(request: Request):
    """
    Receive and process incoming Instagram DM events.
    Meta sends one POST per batch of events.
    """
    config = get_config()
    body_bytes = await request.body()

    # Verify the request signature
    if not _verify_signature(
        body_bytes,
        request.headers.get("X-Hub-Signature-256", ""),
        config.meta_app_secret,
    ):
        logger.warning("Invalid Instagram webhook signature.")
        raise HTTPException(status_code=403, detail="Invalid signature")

    body = await request.json()

    # Instagram webhooks have object = "instagram"
    if body.get("object") != "instagram":
        return Response(status_code=200)

    for entry in body.get("entry", []):
        # Each entry can have multiple messaging events
        for event in entry.get("messaging", []):
            await _process_event(event)

    # Always return 200 quickly
    return Response(content="EVENT_RECEIVED", status_code=200)


# ── Event processing ─────────────────────────────────────────

async def _process_event(event: dict):
    """
    Route an Instagram messaging event to the appropriate handler.
    Events can be messages, postbacks, referrals, or read receipts.
    """
    sender_id = event.get("sender", {}).get("id", "")
    recipient_id = event.get("recipient", {}).get("id", "")
    sender = event.get("sender", {}) or {}
    sender_profile = {
        "display_name": (sender.get("name") or sender.get("username") or "").strip() or None,
        "instagram_handle": (sender.get("username") or "").strip().lstrip("@") or None,
    }

    # Skip echo messages (messages we sent, echoed back to us)
    if event.get("message", {}).get("is_echo"):
        return

    # Skip read receipts
    if "read" in event:
        return

    # Skip delivery confirmations
    if "delivery" in event:
        return

    # Handle message deletions (acknowledge but don't act)
    if event.get("message", {}).get("is_deleted"):
        logger.info(f"Instagram: message deleted by {_mask_sender(sender_id)}")
        return

    # ── Regular messages ─────────────────────────────────────
    if "message" in event:
        await _process_message(sender_id, event["message"], sender_profile)
        return

    # ── Postbacks (Ice Breaker taps, button clicks) ──────────
    if "postback" in event:
        await _process_postback(sender_id, event["postback"], sender_profile)
        return

    # ── Referrals (ad clicks, link clicks that open DM) ──────
    if "referral" in event:
        await _process_referral(sender_id, event["referral"], sender_profile)
        return

    logger.debug(f"Instagram: unhandled event type from {sender_id}")


async def _process_message(sender_id: str, message: dict, sender_profile: dict | None = None):
    """Process a regular text or media message."""
    text = ""
    media_url = None

    # Text message
    if "text" in message:
        text = message["text"]

    # Quick Reply callback (user tapped a Quick Reply pill)
    elif "quick_reply" in message:
        payload = message["quick_reply"].get("payload", "")
        text = message.get("text", payload)

    # Attachments (images, audio, video, shares)
    elif "attachments" in message:
        for attachment in message["attachments"]:
            att_type = attachment.get("type", "")

            if att_type == "image":
                media_url = attachment.get("payload", {}).get("url")
                text = "El cliente envió una imagen."

            elif att_type == "story_mention":
                text = "El cliente te mencionó en su historia de Instagram."

            elif att_type == "story_reply":
                text = message.get("reply_to", {}).get("story", {}).get("text", "")
                if not text:
                    text = "El cliente respondió a tu historia."

            elif att_type in ("audio", "video", "file"):
                media_url = attachment.get("payload", {}).get("url")
                text = f"[El cliente envió un archivo de tipo: {att_type}]"

            elif att_type == "share":
                # Shared a post, reel, or profile
                share_url = attachment.get("payload", {}).get("url", "")
                text = f"El cliente compartió un enlace: {share_url}" if share_url else "El cliente compartió contenido."

            else:
                text = f"[Contenido de tipo no soportado: {att_type}]"

    if not text.strip():
        return

    logger.info(f"Instagram DM received from {_mask_sender(sender_id)}")
    await _route_to_ai(sender_id, text, media_url, sender_profile)


async def _process_postback(sender_id: str, postback: dict, sender_profile: dict | None = None):
    """
    Process a postback event (Ice Breaker tap or button click).
    Convert the payload into a natural language message for the AI.
    """
    payload = postback.get("payload", "")
    title = postback.get("title", "")

    # Check if this is a known Ice Breaker payload
    context = ICE_BREAKER_CONTEXT.get(payload)
    if context:
        text = context
    elif title:
        text = title
    else:
        text = f"[Postback: {payload}]"

    logger.info(f"Instagram postback from {_mask_sender(sender_id)}")
    await _route_to_ai(sender_id, text, sender_profile=sender_profile)


async def _process_referral(sender_id: str, referral: dict, sender_profile: dict | None = None):
    """
    Process a referral event (customer came from an ad or link).
    Send a warm welcome that acknowledges where they came from.
    """
    source = referral.get("source", "")
    ref_type = referral.get("type", "")
    ad_title = referral.get("ads_context_data", {}).get("ad_title", "")

    if ad_title:
        text = f"Hola, vine por el anuncio: {ad_title}"
    elif source == "ADS":
        text = "Hola, vine por un anuncio de Instagram."
    else:
        text = "Hola, quiero más información."

    logger.info(f"Instagram referral from {_mask_sender(sender_id)}: source={source}")
    await _route_to_ai(sender_id, text, sender_profile=sender_profile)


# ── AI routing ───────────────────────────────────────────────

async def _route_to_ai(
    sender_id: str,
    text: str,
    media_url: str | None = None,
    sender_profile: dict | None = None,
):
    await enqueue_inbound_message(
        channel="instagram",
        sender_id=sender_id,
        text=text,
        media_url=media_url,
        customer_profile=sender_profile or {},
        processor=_deliver_ai_response,
    )


async def _deliver_ai_response(
    sender_id: str,
    text: str,
    media_url: str | None = None,
    sender_profile: dict | None = None,
):
    """
    Route the normalized message through the AI engine
    and send the response back via Instagram DM.
    """
    try:
        result = await generate_response(
            channel="instagram",
            sender_id=sender_id,
            message_text=text,
            media_url=media_url,
            customer_profile=sender_profile or {},
        )
    except Exception as e:
        logger.exception(f"Error generating Instagram response for {_mask_sender(sender_id)}: {e}")
        await _safe_send_apology(sender_id)
        return

    if result.get("paused"):
        return
    if result.get("escalated") and not (
        result.get("text") or result.get("interactive") or result.get("catalog_pdf") or result.get("product_image")
    ):
        return

    try:
        if result.get("interactive") and result["interactive"].get("type") == "interactive_buttons":
            quick_replies = [
                {"title": btn, "payload": btn.upper().replace(" ", "_")}
                for btn in result["interactive"]["buttons"]
            ]
            await send_text_with_quick_replies(
                to=sender_id,
                text=result["interactive"]["body_text"],
                quick_replies=quick_replies,
            )
        elif result.get("product_image") and result["product_image"].get("type") == "product_image":
            await send_image(
                to=sender_id,
                image_url=result["product_image"]["image_url"],
            )
            follow_up = (result.get("text") or result["product_image"].get("caption") or "").strip()
            if follow_up:
                if len(follow_up.encode("utf-8")) > 950:
                    chunks = _split_message(follow_up, max_bytes=950)
                    for chunk in chunks:
                        await send_text(to=sender_id, text=chunk)
                else:
                    await send_text(to=sender_id, text=follow_up)
        elif result.get("text"):
            reply = result["text"]
            if len(reply.encode("utf-8")) > 950:
                chunks = _split_message(reply, max_bytes=950)
                for chunk in chunks:
                    await send_text(to=sender_id, text=chunk)
            else:
                await send_text(to=sender_id, text=reply)
    except Exception as e:
        logger.exception(f"Error sending Instagram response to {_mask_sender(sender_id)}: {e}")
        await _notify_delivery_failure(sender_id, str(e))


# ── Helpers ──────────────────────────────────────────────────

def _split_message(text: str, max_bytes: int = 950) -> list[str]:
    """
    Split a message into chunks that fit within Instagram's byte limit.
    Tries to split at sentence boundaries for natural reading.
    """
    chunks = []
    current = ""

    sentences = text.replace(". ", ".|").replace("! ", "!|").replace("? ", "?|").split("|")

    for sentence in sentences:
        test = (current + " " + sentence).strip() if current else sentence
        if len(test.encode("utf-8")) <= max_bytes:
            current = test
        else:
            if current:
                chunks.append(current)
            current = sentence

    if current:
        chunks.append(current)

    return chunks if chunks else [text[:max_bytes]]


def _verify_signature(body: bytes, signature_header: str, app_secret: str) -> bool:
    """Verify the X-Hub-Signature-256 header from Meta."""
    if not signature_header or not app_secret:
        return False

    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature_header)
