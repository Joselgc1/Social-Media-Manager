"""
WhatsApp webhook handler.
Receives incoming messages from the WhatsApp Cloud API,
normalizes them, and routes them through the AI engine.
"""

import hashlib
import json
import logging
from contextlib import suppress

from fastapi import APIRouter, HTTPException, Request, Response

from app.admin.notify import notify_owner
from app.ai.engine import generate_response
from app.channels.whatsapp_sender import mark_as_read, send_document, send_image, send_interactive_buttons, send_text
from app.config import get_config
from app.crm import conversations
from app.webhooks.inbound_buffer import enqueue_inbound_message, send_with_delivery_record
from app.webhooks.meta_security import verify_meta_signature

logger = logging.getLogger(__name__)
router = APIRouter()
_APOLOGY_TEXT = "Disculpa, tuve un problema procesando tu mensaje. ¿Puedes intentar de nuevo en un momento? 🙏"


def _mask_sender(sender: str) -> str:
    if len(sender) <= 4:
        return sender
    return f"{sender[:2]}***{sender[-2:]}"


async def _notify_delivery_failure(sender: str, detail: str):
    await notify_owner(
        f"⚠️ *Fallo enviando respuesta por WhatsApp*\n\n"
        f"*Cliente:* `{_mask_sender(sender)}`\n"
        f"*Detalle:* {detail[:400]}"
    )


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
    msg_id = message.get("id", "") or _message_fingerprint(message)
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

    logger.info(f"WhatsApp message received from {_mask_sender(sender)} ({msg_type})")

    created = await enqueue_inbound_message(
        channel="whatsapp",
        sender_id=sender,
        message_id=msg_id,
        text=text,
        media_url=media_url,
        customer_profile={
            "display_name": display_name,
            "phone": sender,
        },
    )
    if not created:
        logger.info("Ignoring duplicate WhatsApp message %s from %s", msg_id, _mask_sender(sender))
        return

    # Mark read only after the delivery is durably committed.
    with suppress(Exception):
        await mark_as_read(msg_id)


async def _deliver_ai_response(
    sender: str,
    text: str,
    media_url: str | None,
    customer_profile: dict,
    inbound_job_id: str = "",
    lease_token: str = "",
):
    # Route through the AI engine
    try:
        result = await generate_response(
            channel="whatsapp",
            sender_id=sender,
            message_text=text,
            media_url=media_url,
            customer_profile=customer_profile,
            persist_assistant_message=False,
            persist_user_before_response=True,
            message_source_id=inbound_job_id or None,
        )
    except Exception as e:
        logger.exception(f"Error generating WhatsApp response for {_mask_sender(sender)}: {e}")
        try:
            await _send_with_delivery_record(send_text, inbound_job_id, lease_token, to=sender, text=_APOLOGY_TEXT)
        except Exception as send_error:
            logger.error(f"Failed to send WhatsApp fallback reply to {_mask_sender(sender)}: {send_error}")
            raise
        return

    if result.get("paused"):
        return
    if result.get("escalated") and not (
        result.get("text") or result.get("interactive") or result.get("catalog_pdf") or result.get("product_image")
    ):
        return

    delivered_parts = []
    try:
        if result.get("catalog_pdf") and result["catalog_pdf"].get("type") == "catalog_pdf":
            pdf_url = f"{get_config().app_base_url}/static/catalog/catalog.pdf"
            follow_up = (result.get("text") or result["catalog_pdf"].get("caption") or "").strip()
            await _send_with_delivery_record(
                send_document,
                inbound_job_id,
                lease_token,
                to=sender,
                document_url=pdf_url,
                filename="Catalogo VS.pdf",
                caption="",
            )
            delivered_parts.append("[Catálogo PDF enviado]")
            if follow_up:
                await _send_with_delivery_record(send_text, inbound_job_id, lease_token, to=sender, text=follow_up)
                delivered_parts.append(follow_up)
        elif result.get("interactive") and result["interactive"].get("type") == "interactive_buttons":
            await _send_with_delivery_record(
                send_interactive_buttons,
                inbound_job_id,
                lease_token,
                to=sender,
                body_text=result["interactive"]["body_text"],
                buttons=result["interactive"]["buttons"],
            )
            delivered_parts.append(result["interactive"]["body_text"])
        elif result.get("product_image") and result["product_image"].get("type") == "product_image":
            caption = result["product_image"].get("caption", "")
            await _send_with_delivery_record(
                send_image,
                inbound_job_id,
                lease_token,
                to=sender,
                image_url=result["product_image"]["image_url"],
                caption=caption,
            )
            delivered_parts.append(caption.strip() or "[Imagen de producto enviada]")
            follow_up = (result.get("text") or "").strip()
            if follow_up and follow_up != caption.strip():
                await _send_with_delivery_record(send_text, inbound_job_id, lease_token, to=sender, text=follow_up)
                delivered_parts.append(follow_up)
        elif result.get("text"):
            await _send_with_delivery_record(send_text, inbound_job_id, lease_token, to=sender, text=result["text"])
            delivered_parts.append(result["text"])
    except Exception as e:
        logger.exception(f"Error sending WhatsApp response to {_mask_sender(sender)}: {e}")
        await _notify_delivery_failure(sender, str(e))
        raise

    if delivered_parts:
        await _store_delivered_assistant_message(result, "\n".join(delivered_parts), inbound_job_id)


async def _send_with_delivery_record(send_func, inbound_job_id: str, lease_token: str, **kwargs):
    return await send_with_delivery_record(send_func, inbound_job_id, lease_token, **kwargs)


async def _store_delivered_assistant_message(result: dict, content: str, source_id: str) -> None:
    try:
        await conversations.store_message(
            customer_id=result["customer_id"],
            role="assistant",
            content=content,
            channel="whatsapp",
            function_calls=result.get("function_calls"),
            source_id=source_id or None,
        )
    except Exception:
        logger.exception("Failed to persist delivered WhatsApp response for job %s", source_id)


# ── Signature verification ───────────────────────────────────

_verify_signature = verify_meta_signature


def _message_fingerprint(message: dict) -> str:
    canonical = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "fallback-" + hashlib.sha256(canonical.encode()).hexdigest()
