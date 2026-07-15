"""
Deterministic pre-model guard policies.
"""

from __future__ import annotations

import re
import unicodedata

_HOSTILE_MESSAGE_PATTERNS = [
    (
        re.compile(r"\b(estafa|estafadores?|ladrones?|fraude|timador(?:es)?|robo)\b"),
        "Cliente acusa a la tienda de estafa o robo.",
    ),
    (
        re.compile(
            r"\b(maldit[oa]s?|idiot[ae]s?|imbecil(?:es)?|estupid[oa]s?|"
            r"basura|porqueria|inutil(?:es)?|payas[oa]s?|mierda|asqueros[oa]s?)\b"
        ),
        "Cliente usa insultos o lenguaje agresivo hacia la tienda.",
    ),
    (
        re.compile(
            r"(voy a denunciar|te voy a denunciar|los voy a denunciar|"
            r"voy a demandar|te voy a demandar|los voy a demandar|"
            r"voy a quemar|te voy a quemar|los voy a quemar|"
            r"voy a funar|te voy a exponer|los voy a exponer|"
            r"me las van a pagar|les voy a caer)"
        ),
        "Cliente usa amenazas o lenguaje agresivo.",
    ),
]

_HUMAN_REQUEST_PATTERNS = (
    re.compile(r"\b(humano|persona real|asesor(?:a)?|encargad[oa]|dueñ[oa]|supervisor(?:a)?)\b"),
    re.compile(r"\b(quiero|necesito|puedo|puedes|me puedes|me pasas|pasame|ponme)\b.*\b(hablar|atenderme|atienda|comunicarme)\b"),
)


def normalize_text_for_moderation(text: str) -> str:
    """Normalize text for deterministic guard matching."""
    normalized = unicodedata.normalize("NFKD", (text or "").lower())
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip()


def is_ai_paused(settings: dict) -> bool:
    """Return whether AI replies are globally paused."""
    return not settings.get("ai_enabled", True)


def is_customer_escalated(customer: dict) -> bool:
    """Return whether a customer is already escalated to a human."""
    return customer.get("conversation_state") == "escalated"


def is_customer_blocked(customer: dict) -> bool:
    """Return whether a customer is blocked from automated replies."""
    return bool(customer.get("is_blocked")) or customer.get("conversation_state") == "blocked"


def detect_hostile_customer_message(message_text: str) -> str | None:
    """Return a high-confidence hostility reason, if present."""
    normalized = normalize_text_for_moderation(message_text)
    for pattern, reason in _HOSTILE_MESSAGE_PATTERNS:
        if pattern.search(normalized):
            return reason
    return None


def detect_human_request(message_text: str) -> str | None:
    """Return a deterministic reason when the customer explicitly requests a human."""
    normalized = normalize_text_for_moderation(message_text)
    if not normalized:
        return None
    if any(pattern.search(normalized) for pattern in _HUMAN_REQUEST_PATTERNS):
        return "Cliente pide hablar con una persona del equipo."
    return None


def looks_like_payment_proof_message(message_text: str, vision_result: dict) -> bool:
    """Return whether text or vision output indicates a payment proof attempt."""
    normalized_message = normalize_text_for_moderation(message_text)
    if any(
        hint in normalized_message
        for hint in ("pago", "pague", "pagado", "comprobante", "captura", "capture", "transferencia", "zelle", "binance", "zinli")
    ):
        return True

    detected_method = str(vision_result.get("payment_method", "") or "").strip().lower()
    detected_amount = str(vision_result.get("amount", "") or "").strip()
    detected_status = str(vision_result.get("status", "") or "").strip().lower()
    recipient_identifier = str(vision_result.get("recipient_identifier", "") or "").strip()
    reference = str(vision_result.get("reference", "") or "").strip()

    return bool(
        detected_method not in {"", "unknown"}
        or detected_amount
        or detected_status in {"completed", "pending", "failed"}
        or recipient_identifier
        or reference
    )


def is_payment_proof_without_order(payment_proof_attempt: bool, open_order: dict | None) -> bool:
    """Return whether payment proof was sent before any open order exists."""
    return payment_proof_attempt and not open_order
