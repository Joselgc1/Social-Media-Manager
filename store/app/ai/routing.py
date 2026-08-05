"""
Deterministic routing contract for future multi-agent orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.ai.policies import guards
from app.ai.policies.channel_capabilities import get_channel_capabilities

AgentRoute = Literal[
    "legacy",
    "sales",
    "checkout",
    "payment",
    "support",
    "direct",
]


@dataclass(frozen=True)
class RouteDecision:
    """Structured routing decision for an inbound message."""

    route: AgentRoute
    intent: str
    confidence: float
    source: str
    reason: str


def decide_route(
    message_text: str,
    *,
    channel: str = "whatsapp",
    payment_proof_attempt: bool = False,
    vision_result: dict | None = None,
    session_state: Any | None = None,
) -> RouteDecision:
    """Make a deterministic route decision without executing any agent."""
    hostility_reason = guards.detect_hostile_customer_message(message_text)
    if hostility_reason:
        return RouteDecision(
            route="support",
            intent="hostile_message",
            confidence=1.0,
            source="deterministic_guard",
            reason=hostility_reason,
        )

    human_reason = guards.detect_human_request(message_text)
    if human_reason:
        return RouteDecision(
            route="support",
            intent="human_request",
            confidence=0.95,
            source="deterministic_guard",
            reason=human_reason,
        )

    capabilities = get_channel_capabilities(channel)
    normalized = guards.normalize_text_for_moderation(message_text)
    active_agent = _session_value(session_state, "active_agent")
    workflow_stage = _session_value(session_state, "workflow_stage")

    if capabilities.informational_only:
        support_intent = _detect_support_intent(normalized)
        if support_intent in {"complaint_or_refund", "payment_dispute", "delivery_issue", "tracking_question"}:
            return RouteDecision(
                route="support",
                intent=support_intent,
                confidence=0.8,
                source="deterministic_keyword",
                reason="La conversación parece una consulta de soporte, pedido existente o reclamo.",
            )
        if _looks_like_pdf_catalog_request(normalized):
            return RouteDecision(
                route="sales",
                intent="instagram_catalog_pdf_handoff",
                confidence=0.9,
                source="channel_policy",
                reason="El catálogo PDF se entrega mediante el canal transaccional de WhatsApp.",
            )
        if payment_proof_attempt or _looks_like_transaction_intent(normalized):
            return RouteDecision(
                route="sales",
                intent="instagram_whatsapp_handoff",
                confidence=0.95 if payment_proof_attempt else 0.9,
                source="channel_policy",
                reason="Instagram permite información de productos, pero no flujos transaccionales.",
            )
        if _looks_like_handoff_decline_or_close(normalized):
            return RouteDecision(
                route="sales",
                intent="conversation_close",
                confidence=0.9,
                source="channel_policy",
                reason="La cliente rechazó el cambio de canal o cerró la conversación.",
            )
        if active_agent == "checkout":
            return RouteDecision(
                route="sales",
                intent="instagram_whatsapp_handoff",
                confidence=0.9,
                source="channel_policy",
                reason="Instagram permite información de productos, pero no flujos transaccionales.",
            )
        if support_intent:
            return RouteDecision(
                route="support",
                intent=support_intent,
                confidence=0.8,
                source="deterministic_keyword",
                reason="La conversación parece una consulta de soporte, pedido existente o reclamo.",
            )

    if payment_proof_attempt:
        return RouteDecision(
            route="payment",
            intent="payment_proof",
            confidence=0.95 if vision_result else 0.85,
            source="deterministic_guard",
            reason="Cliente parece haber enviado un comprobante de pago.",
        )

    if _looks_like_checkout_cancellation(normalized) and active_agent == "checkout":
        return RouteDecision(
            route="checkout",
            intent="cancel_checkout",
            confidence=0.9,
            source="session_guard",
            reason="Cliente quiere cancelar el checkout activo.",
        )

    if active_agent == "checkout" and workflow_stage in {"checkout_collecting", "checkout_ready"}:
        return RouteDecision(
            route="checkout",
            intent="checkout_followup",
            confidence=0.9,
            source="session_sticky",
            reason="La sesión tiene un checkout activo que debe continuar.",
        )

    if active_agent == "support" and _looks_like_support_followup(normalized):
        return RouteDecision(
            route="support",
            intent="support_followup",
            confidence=0.85,
            source="session_sticky",
            reason="La sesión reciente de soporte sigue activa y el mensaje parece seguimiento.",
        )

    if _looks_like_purchase_intent(normalized):
        return RouteDecision(
            route="checkout",
            intent="checkout_or_order",
            confidence=0.75,
            source="deterministic_keyword",
            reason="La conversación expresa intención clara de compra o pedido.",
        )

    support_intent = _detect_support_intent(normalized)
    if support_intent:
        return RouteDecision(
            route="support",
            intent=support_intent,
            confidence=0.8,
            source="deterministic_keyword",
            reason="La conversación parece una consulta de soporte, pedido existente o reclamo.",
        )

    sales_intent = _detect_sales_intent(normalized)
    if sales_intent:
        return RouteDecision(
            route="sales",
            intent=sales_intent,
            confidence=0.7,
            source="deterministic_keyword",
            reason="La conversación parece una consulta de ventas, catálogo o producto.",
        )

    return RouteDecision(
        route="legacy",
        intent="general",
        confidence=0.5,
        source="default",
        reason="No deterministic specialist route matched.",
    )


def _session_value(session_state: Any | None, key: str) -> str | None:
    if session_state is None:
        return None
    value = session_state.get(key) if isinstance(session_state, dict) else getattr(session_state, key, None)
    return str(value or "").strip().lower() or None


def _looks_like_checkout_cancellation(normalized: str) -> bool:
    return any(marker in normalized for marker in ("cancelar", "cancela", "olvida", "ya no", "no quiero", "dejalo"))


def _looks_like_purchase_intent(normalized: str) -> bool:
    strong_markers = (
        "quiero comprar",
        "quiero pedir",
        "quiero llevar",
        "quiero ese",
        "quiero esa",
        "lo quiero",
        "la quiero",
        "me llevo",
        "me lo llevo",
        "me la llevo",
        "lo compro",
        "la compro",
        "comprame",
        "armame el pedido",
        "hacer pedido",
        "crear pedido",
        "confirmo el pedido",
        "te paso mi direccion",
        "mi direccion es",
        "voy a pagar",
        "quiero pagar",
        "para pagar",
    )
    return any(marker in normalized for marker in strong_markers)


def _looks_like_transaction_intent(normalized: str) -> bool:
    if _looks_like_purchase_intent(normalized):
        return True
    transaction_markers = (
        "finalizar compra",
        "completar compra",
        "confirmar compra",
        "confirmar pedido",
        "hacer checkout",
        "datos para pagar",
        "datos de pago",
        "como pago",
        "como puedo pagar",
        "pasame el zelle",
        "pago movil",
        "pago con",
        "te paso la direccion",
        "enviar a mi direccion",
        "mandalo por mrw",
        "mandalo por zoom",
        "retiro en agencia",
        "mi agencia es",
    )
    return any(marker in normalized for marker in transaction_markers)


def _looks_like_pdf_catalog_request(normalized: str) -> bool:
    if "catalogo" not in normalized and "pdf" not in normalized:
        return False
    request_markers = (
        "mandame",
        "me mandas",
        "pasame",
        "me pasas",
        "enviame",
        "me envias",
        "quiero ver",
        "quiero recibir",
        "quiero el",
        "quiero la",
        "muestrame",
        "dame",
    )
    return any(marker in normalized for marker in request_markers)


def _looks_like_handoff_decline_or_close(normalized: str) -> bool:
    close_markers = ("no gracias", "ya no", "no quiero", "dejalo", "olvida", "tranqui", "gracias")
    return any(marker in normalized for marker in close_markers)


def _looks_like_support_followup(normalized: str) -> bool:
    if not normalized:
        return False
    if _detect_support_intent(normalized):
        return True
    followup_markers = (
        "cuando llega",
        "cuando me llega",
        "que hago",
        "que puedo hacer",
        "y entonces",
        "todavia nada",
        "sigue igual",
        "no aparece",
        "no me sale",
        "la guia",
        "el tracking",
        "mi pago",
        "mi paquete",
    )
    return any(marker in normalized for marker in followup_markers)


def _detect_sales_intent(normalized: str) -> str | None:
    if not normalized:
        return None
    if normalized in {"hola", "buenas", "buenos dias", "buenas tardes", "buenas noches", "hello", "hi"}:
        return "greeting"
    if any(marker in normalized for marker in ("catalogo", "que tienes", "que tienen", "muestrame todo", "ver todo")):
        return "catalog_request"
    if any(marker in normalized for marker in ("foto", "imagen", "verlo", "verla", "como se ve")):
        return "product_photo_request"
    if any(marker in normalized for marker in ("recomienda", "recomendacion", "que me sugieres", "cual me recomiendas")):
        return "recommendation_request"
    if any(marker in normalized for marker in ("talla", "size")):
        return "size_question"
    if any(marker in normalized for marker in ("disponible", "tienes", "hay", "queda", "agotado")):
        return "availability_question"
    if any(marker in normalized for marker in ("precio", "cuanto", "pijama", "panty", "set", "splash", "brasier", "encaje")):
        return "product_search"
    if any(marker in normalized for marker in ("envio", "envios", "mrw", "zoom", "zelle", "binance", "zinli", "metodos de pago", "formas de pago")):
        return "general_product_info"
    return None


def _detect_support_intent(normalized: str) -> str | None:
    if not normalized:
        return None
    if any(marker in normalized for marker in ("estado de mi pedido", "mi pedido", "pedido pendiente", "orden")):
        return "order_status"
    if any(marker in normalized for marker in ("tracking", "guia", "rastreo", "numero de guia", "donde va", "por donde va")):
        return "tracking_question"
    if any(marker in normalized for marker in ("no me ha llegado", "no llego", "paquete", "entrega", "retraso")):
        return "delivery_issue"
    if any(marker in normalized for marker in ("reembolso", "devolucion", "cambio", "garantia", "reclamo", "queja")):
        return "complaint_or_refund"
    if any(marker in normalized for marker in ("pago rechazado", "pago no aparece", "me cobraron", "disputa")):
        return "payment_dispute"
    return None
