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
    if result.status == "ambiguous_order":
        return (
            f"{prefix}veo más de un pedido abierto y no quiero aplicar el comprobante al pedido equivocado. "
            "Necesito que revisemos cuál pedido corresponde antes de confirmar el pago."
        )
    if result.status == "unreadable":
        return f"{prefix}no pude leer bien el comprobante. ¿Me envías una captura más clara, porfa?"
    if result.status == "amount_mismatch":
        expected = _format_amount(result.expected_amount, result.expected_currency)
        detected = _format_amount(result.detected_amount, result.detected_currency)
        return f"{prefix}el monto del comprobante no coincide con el pedido. Esperado: {expected}. Detectado: {detected}."
    if result.status == "currency_mismatch":
        return f"{prefix}la moneda del comprobante no coincide con el método de pago del pedido. Lo revisamos antes de confirmarlo."
    if result.status == "method_mismatch":
        return f"{prefix}el comprobante parece ser de un método distinto al que quedó en el pedido. Lo revisamos antes de confirmarlo."
    if result.status == "recipient_mismatch":
        return f"{prefix}el comprobante no coincide con los datos configurados para ese método de pago. Lo revisamos antes de confirmarlo."
    if result.status == "not_completed":
        return f"{prefix}el comprobante no aparece como pago completado. Cuando salga completado, envíame la captura actualizada."
    if result.status == "duplicate_proof":
        return f"{prefix}ese comprobante ya fue usado para otro pedido. Necesito revisarlo manualmente."
    return f"{prefix}necesito revisar ese comprobante manualmente antes de confirmar el pago."


def _format_amount(amount, currency: str | None = None) -> str:
    if amount is None:
        return "no leído"
    symbol = {"USD": "$", "VES": "Bs ", "USDT": "USDT "}.get(currency or "USD", "")
    return f"{symbol}{amount:.2f}"
