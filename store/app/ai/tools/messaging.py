"""
Messaging and escalation tool handlers.
"""

from __future__ import annotations

import logging
import re
import unicodedata

from app.admin.notify import notify_escalation
from app.ai.tools.context import ToolExecutionContext
from app.catalog.pdf_generator import ensure_catalog_pdf
from app.catalog.sheets import get_cached_catalog
from app.config import get_config
from app.crm import conversations, escalations

logger = logging.getLogger(__name__)


async def escalate_to_human(args: dict, context: ToolExecutionContext) -> dict:
    """Escalate a conversation unless it is only a normal product-availability inquiry."""
    customer = context.customer
    customer_id = customer["id"]

    if should_block_product_inquiry_escalation(
        reason=args.get("reason", ""),
        latest_user_message=context.latest_user_message,
    ):
        return {
            "status": "error",
            "message": (
                "No escales preguntas normales de productos no disponibles o fuera del catálogo. "
                "Explica que no lo manejan o que está agotado y ofrece alternativas del catálogo."
            ),
        }

    summary = await conversations.get_recent_summary(customer_id, limit=5)
    await notify_escalation(
        customer_name=customer.get("display_name"),
        customer_channel=customer.get("channel", "whatsapp"),
        customer_platform_id=customer.get("platform_id", ""),
        reason=args.get("reason", "Razón no especificada"),
        urgency=args.get("urgency", "medium"),
        conversation_summary=summary,
    )
    await escalations.escalate_customer_automatically(customer_id)
    await _sync_kommo_escalation_if_needed(
        customer_id=customer_id,
        reason=args.get("reason", "Razón no especificada"),
        urgency=args.get("urgency", "medium"),
        conversation_summary=summary,
        lead_id=(context.integration_context or {}).get("lead_id"),
    )
    return {
        "status": "escalated",
        "message": "The store owner has been notified and will respond shortly.",
    }


async def send_interactive_buttons(args: dict, context: ToolExecutionContext) -> dict:
    """Return an interactive-button payload for the channel sender."""
    return {
        "type": "interactive_buttons",
        "body_text": args.get("body_text", ""),
        "buttons": args.get("buttons", []),
    }


async def send_catalog_pdf(args: dict, context: ToolExecutionContext) -> dict:
    """Ensure the catalog PDF exists and return a PDF payload for the channel sender."""
    if not _catalog_pdf_supported(context.channel, context.integration_context):
        return {
            "status": "error",
            "message": "Catalog PDF delivery is unavailable here. Describe catalog categories in text instead.",
        }
    catalog = get_cached_catalog()
    if not catalog:
        return {"status": "error", "message": "Catalog is empty, cannot generate PDF."}
    try:
        ensure_catalog_pdf(catalog)
    except Exception as e:
        logger.error(f"Auto-generate catalog PDF failed: {e}")
        return {"status": "error", "message": "Could not generate catalog PDF."}
    return {
        "type": "catalog_pdf",
        "caption": args.get("caption", "Aqui tienes nuestro catalogo de productos 📖"),
    }


async def request_agent_handoff(args: dict, context: ToolExecutionContext) -> dict:
    """Return structured internal handoff metadata without changing customer-visible state."""
    target_agent = str(args.get("target_agent") or "").strip().lower()
    if target_agent not in {"checkout"}:
        return {"status": "error", "message": f"Unsupported handoff target: {target_agent}"}
    return {
        "type": "agent_handoff",
        "target_agent": target_agent,
        "intent": str(args.get("intent") or "purchase_intent").strip() or "purchase_intent",
        "reason": str(args.get("reason") or "").strip(),
    }


def normalize_text_for_moderation(text: str) -> str:
    """Normalize text for high-confidence escalation guard matching."""
    normalized = unicodedata.normalize("NFKD", (text or "").lower())
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip()


def should_block_product_inquiry_escalation(reason: str, latest_user_message: str) -> bool:
    """Return True when an escalation request is only an unavailable-product inquiry."""
    normalized_reason = normalize_text_for_moderation(reason)
    normalized_message = normalize_text_for_moderation(latest_user_message)

    if not normalized_reason:
        return False

    complaint_markers = (
        "reembolso", "refund", "devolucion", "demanda", "denuncia", "estafa",
        "molesto", "amenaza", "humano", "persona real", "supervisor", "gerente",
        "pago", "comprobante", "disputa", "cancelar pedido", "envio retrasado",
    )
    if any(marker in normalized_reason for marker in complaint_markers):
        return False

    product_reason_markers = (
        "catalog", "catalogo", "no aparece", "no esta", "no se encuentra",
        "no puedo confirmar", "no hay", "agotad", "sin stock", "sin inventario",
        "producto", "precio", "talla", "disponibil",
    )
    message_product_markers = (
        "tienes", "talla", "precio", "disponible", "hay", "busco", "quiero",
        "pant", "pijama", "conjunto", "set", "encaje", "splash", "victoria",
        "ropa interior", "underwear",
    )

    reason_is_product_related = any(marker in normalized_reason for marker in product_reason_markers)
    message_is_product_related = any(marker in normalized_message for marker in message_product_markers)

    return reason_is_product_related and message_is_product_related


def _catalog_pdf_supported(channel: str, integration_context: dict | None) -> bool:
    config = get_config()
    delivery_provider = (integration_context or {}).get("provider")
    if not delivery_provider:
        delivery_provider = getattr(config, "channel_backend", "meta")
    if not isinstance(delivery_provider, str) or delivery_provider not in {"meta", "kommo"}:
        delivery_provider = "meta"
    if channel != "whatsapp":
        return False
    if delivery_provider == "meta":
        return True
    return (
        (integration_context or {}).get("interaction_type", "private_message")
        == "private_message"
        and bool(getattr(config, "kommo_chats_media_enabled", False))
        and bool(getattr(config, "kommo_chats_catalog_pdf_enabled", False))
        and getattr(config, "kommo_chats_pdf_attachment_type", None) == "file"
    )


async def _sync_kommo_escalation_if_needed(
    *,
    customer_id: str,
    reason: str,
    urgency: str,
    conversation_summary: str,
    lead_id: str | None = None,
) -> None:
    if getattr(get_config(), "channel_backend", "meta") != "kommo":
        return
    from app.integrations.kommo.state import sync_escalation_to_kommo

    await sync_escalation_to_kommo(
        customer_id=customer_id,
        reason=reason,
        urgency=urgency,
        conversation_summary=conversation_summary,
        lead_id=lead_id,
    )
