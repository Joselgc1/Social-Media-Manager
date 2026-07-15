"""Customer-facing wording for deterministic payment verification results."""

from __future__ import annotations

from app.ai.payment.models import PaymentVerificationResult
from app.customer_identity import extract_safe_first_name


def render_payment_response(result: PaymentVerificationResult, customer: dict | None = None) -> str:
    """Return fixed customer-facing text for a verification result."""
    first_name = extract_safe_first_name((customer or {}).get("display_name"))
    prefix = f"{first_name}, " if first_name else ""

    if result.status == "verified":
        return f"{prefix}recibí el comprobante y coincide con tu pedido. Ya quedó marcado para revisión final."
    if result.status == "no_open_order":
        return (
            f"{prefix}todavía no tengo el pedido registrado para poder validar ese comprobante. "
            "Primero dejamos el pedido armado y luego seguimos con el pago."
        )
    if result.status == "unreadable":
        return f"{prefix}no pude leer bien el comprobante. ¿Me envías una captura más clara, porfa?"
    if result.status == "amount_mismatch":
        expected = _format_amount(result.expected_amount)
        detected = _format_amount(result.detected_amount)
        return f"{prefix}el monto del comprobante no coincide con el pedido. Esperado: {expected}. Detectado: {detected}."
    if result.status == "method_mismatch":
        return f"{prefix}el comprobante parece ser de un método distinto al que quedó en el pedido. Lo revisamos antes de confirmarlo."
    if result.status == "recipient_mismatch":
        return f"{prefix}el comprobante no coincide con los datos configurados para ese método de pago. Lo revisamos antes de confirmarlo."
    if result.status == "not_completed":
        return f"{prefix}el comprobante no aparece como pago completado. Cuando salga completado, envíame la captura actualizada."
    return f"{prefix}necesito revisar ese comprobante manualmente antes de confirmar el pago."


def _format_amount(amount) -> str:
    if amount is None:
        return "no leído"
    return f"${amount:.2f}"
