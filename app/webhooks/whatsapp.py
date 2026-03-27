"""
WhatsApp webhook handler.
Receives incoming messages from the WhatsApp Cloud API,
normalizes them, and routes them through the AI engine.
"""

import hashlib
import hmac
import logging
from fastapi import APIRouter, Request, Response, HTTPException

from app.config import get_config
from app.ai.engine import generate_response
from app.channels.whatsapp_sender import send_text, send_interactive_buttons, send_document, mark_as_read
from app.admin.notify import notify_owner

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Webhook verification (GET) ───────────────────────────────

@router.get("/webhooks/whatsapp")
async def verify_whatsapp(request: Request):
    """
    Meta sends a GET request to verify your webhook URL.
    You must respond with the hub.challenge value.
    """
    config = get_config()
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == config.whatsapp_verify_token:
        logger.info("WhatsApp webhook verified successfully.")
        return Response(content=challenge, status_code=200)

    logger.warning("WhatsApp webhook verification failed.")
    return Response(status_code=403)


# ── Incoming messages (POST) ─────────────────────────────────

@router.post("/webhooks/whatsapp")
async def handle_whatsapp(request: Request):
    """
    Receive and process incoming WhatsApp messages.
    Meta sends one POST per event (message, status update, etc.).
    """
    # Verify the request signature
    config = get_config()
    body_bytes = await request.body()

    if not _verify_signature(body_bytes, request.headers.get("X-Hub-Signature-256", ""), config.meta_app_secret):
        logger.warning("Invalid WhatsApp webhook signature.")
        raise HTTPException(status_code=403, detail="Invalid signature")

    body = await request.json()

    # WhatsApp webhooks always have object = "whatsapp_business_account"
    if body.get("object") != "whatsapp_business_account":
        return Response(status_code=200)

    # Extract messages from the webhook payload
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            # Process actual messages (not status updates)
            messages = value.get("messages", [])
            for message in messages:
                await _process_message(message, value)

    # Always return 200 quickly to avoid Meta retrying
    return Response(content="OK", status_code=200)


# ── Message processing ───────────────────────────────────────

async def _process_message(message: dict, value: dict):
    """
    Process a single incoming WhatsApp message.
    Extracts the sender, text, and media, then routes to the AI engine.
    """
    sender = message.get("from", "")  # Phone number (e.g., "58412XXXXXXX")
    msg_id = message.get("id", "")
    msg_type = message.get("type", "")

    # Extract the display name from contacts if available
    contacts = value.get("contacts", [])
    display_name = contacts[0].get("profile", {}).get("name") if contacts else None

    # Extract message content based on type
    text = ""
    media_url = None

    if msg_type == "text":
        text = message.get("text", {}).get("body", "")

    elif msg_type == "interactive":
        # User tapped a reply button or list item
        interactive = message.get("interactive", {})
        if interactive.get("type") == "button_reply":
            text = interactive.get("button_reply", {}).get("title", "")
        elif interactive.get("type") == "list_reply":
            text = interactive.get("list_reply", {}).get("title", "")

    elif msg_type == "image":
        # Customer sent an image (possibly payment screenshot)
        image = message.get("image", {})
        media_url = image.get("id")  # Media ID, needs another API call to download
        text = image.get("caption", "El cliente envió una imagen.")

    elif msg_type in ("audio", "video", "document", "sticker"):
        text = f"[El cliente envió un archivo de tipo: {msg_type}]"

    elif msg_type == "reaction":
        # Ignore reactions silently
        return

    else:
        text = f"[Mensaje de tipo no soportado: {msg_type}]"

    if not text.strip():
        return

    logger.info(f"WhatsApp message from {sender}: {text[:100]}")

    # Mark the message as read (blue checkmarks)
    try:
        await mark_as_read(msg_id)
    except Exception:
        pass  # Non-critical

    # Route through the AI engine
    try:
        result = await generate_response(
            channel="whatsapp",
            sender_id=sender,
            message_text=text,
            media_url=media_url,
        )

        if result.get("paused") or result.get("escalated"):
            # AI is off — owner handles this conversation directly.
            # Message was already stored and owner notified by the engine.
            return

        # Send the AI's response
        if result.get("catalog_pdf") and result["catalog_pdf"].get("type") == "catalog_pdf":
            pdf_url = f"{get_config().app_base_url}/static/catalog/catalog.pdf"
            await send_document(
                to=sender,
                document_url=pdf_url,
                filename="Catalogo VS.pdf",
                caption=result["catalog_pdf"].get("caption", ""),
            )
            # Also send any accompanying text
            if result.get("text"):
                await send_text(to=sender, text=result["text"])
        elif result.get("interactive") and result["interactive"].get("type") == "interactive_buttons":
            await send_interactive_buttons(
                to=sender,
                body_text=result["interactive"]["body_text"],
                buttons=result["interactive"]["buttons"],
            )
        elif result.get("text"):
            await send_text(to=sender, text=result["text"])

    except Exception as e:
        logger.exception(f"Error processing WhatsApp message from {sender}: {e}")
        # Send a fallback message so the customer isn't left hanging
        await send_text(
            to=sender,
            text="Disculpa, tuve un problema procesando tu mensaje. ¿Puedes intentar de nuevo en un momento? 🙏",
        )


# ── Signature verification ───────────────────────────────────

def _verify_signature(body: bytes, signature_header: str, app_secret: str) -> bool:
    """
    Verify the X-Hub-Signature-256 header to ensure the request
    actually came from Meta and wasn't spoofed.
    """
    if not signature_header:
        return False

    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature_header)
