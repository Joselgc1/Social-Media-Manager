"""Deterministic payment-proof verifier."""

from __future__ import annotations

import logging
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.ai.payment.models import PaymentVerificationResult
from app.ai.tools.catalog import normalize_catalog_text
from app.crm import orders, sessions

AMOUNT_TOLERANCE = Decimal("0.01")
logger = logging.getLogger(__name__)


async def verify_payment_proof(
    customer_id: str,
    payment_methods: list[dict],
    vision_result: dict,
    *,
    confirmation_note: str | None = None,
    update_order: bool = True,
) -> PaymentVerificationResult:
    """Validate a payment screenshot and optionally update the existing order."""
    order = await _get_payment_target_order(customer_id)
    if not order:
        return _result("no_open_order", message="No existe un pedido abierto para validar este comprobante.")

    order_id = str(order.get("id") or order.get("order_id") or "")
    expected_amount = _decimal_amount(order.get("total"))
    expected_method_name = str(order.get("payment_method", "") or "").strip()

    if str(order.get("payment_status") or "").strip().lower() in {"proof_received", "confirmed"}:
        return _result(
            "verified",
            order_id=order_id,
            expected_amount=expected_amount,
            message="El comprobante de este pedido ya fue recibido.",
            payment_method=expected_method_name,
        )

    if not vision_result.get("analyzed"):
        return _result("unreadable", order_id=order_id, expected_amount=expected_amount, message="No se pudo analizar el comprobante.")

    detected_amount = safe_decimal(vision_result.get("amount"))
    if detected_amount is None:
        return _result("unreadable", order_id=order_id, expected_amount=expected_amount, message="No se pudo leer el monto del comprobante.")

    if expected_amount is None or abs(detected_amount - expected_amount) > AMOUNT_TOLERANCE:
        return _result(
            "amount_mismatch",
            order_id=order_id,
            expected_amount=expected_amount,
            detected_amount=detected_amount,
            message="El comprobante no coincide con el monto esperado del pedido.",
        )

    payment_method = find_payment_method(payment_methods, expected_method_name)
    if not payment_method:
        return _result(
            "manual_review",
            order_id=order_id,
            expected_amount=expected_amount,
            detected_amount=detected_amount,
            message="No se encontró la configuración del método de pago del pedido.",
        )

    expected_kind = classify_payment_method_name(expected_method_name)
    detected_kind = str(vision_result.get("payment_method", "") or "").strip().lower()
    if expected_kind and detected_kind not in {"", "unknown"} and detected_kind != expected_kind:
        return _result(
            "method_mismatch",
            order_id=order_id,
            expected_amount=expected_amount,
            detected_amount=detected_amount,
            message="El comprobante parece ser de un método distinto al pedido.",
        )

    identifier_validation = validate_payment_identifier_match(payment_method.get("information", ""), vision_result)
    if not identifier_validation["ok"]:
        return _result(
            "recipient_mismatch",
            order_id=order_id,
            expected_amount=expected_amount,
            detected_amount=detected_amount,
            message=identifier_validation["message"],
        )

    detected_status = str(vision_result.get("status", "") or "").strip().lower()
    if detected_status != "completed":
        return _result(
            "not_completed",
            order_id=order_id,
            expected_amount=expected_amount,
            detected_amount=detected_amount,
            message="El comprobante analizado no aparece como pago completado.",
        )

    if update_order:
        updated = await orders.update_order_payment_status(order_id, status="proof_received", note=confirmation_note)
        if not updated:
            return _result(
                "manual_review",
                order_id=order_id,
                expected_amount=expected_amount,
                detected_amount=detected_amount,
                message="No se pudo actualizar el estado del pedido.",
            )
        await _mark_session_payment_verified(customer_id, order_id)

    return _result(
        "verified",
        order_id=order_id,
        expected_amount=expected_amount,
        detected_amount=detected_amount,
        message="Pago validado correctamente.",
        payment_method=expected_method_name,
    )


def find_payment_method(payment_methods: list[dict], method_name: str) -> dict | None:
    """Find a configured payment method by normalized name."""
    normalized_name = normalize_catalog_text(method_name)
    for payment_method in payment_methods:
        if normalize_catalog_text(payment_method.get("name", "")) == normalized_name:
            return payment_method
    return None


def classify_payment_method_name(method_name: str) -> str | None:
    """Classify a payment method name into a vision-result method kind."""
    normalized = normalize_catalog_text(method_name)
    if "zelle" in normalized:
        return "zelle"
    if "binance" in normalized:
        return "binance"
    if "zinli" in normalized:
        return "zinli"
    if any(token in normalized for token in ("bolivar", "transferencia", "banco", "pago movil")):
        return "bank_transfer"
    return None


def validate_payment_identifier_match(payment_information: str, vision_result: dict) -> dict:
    """Validate proof recipient details without exposing configured credentials."""
    info = (payment_information or "").strip()
    if not info:
        return {"ok": False, "message": "El método de pago no tiene datos configurados para validar el destinatario."}

    vision_text = normalize_catalog_text(
        " ".join(
            str(vision_result.get(key, "") or "")
            for key in ("summary", "raw_response", "recipient_identifier", "recipient_name", "sender_name", "reference")
        )
    )

    expected_emails = {
        email.lower()
        for email in re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", info, flags=re.IGNORECASE)
    }
    if expected_emails:
        if any(email in vision_text for email in expected_emails):
            return {"ok": True}
        return {"ok": False, "message": "El comprobante no muestra el correo o destinatario configurado para este método de pago."}

    collapsed_numeric_info = re.sub(r"(?<=\d)[\s\-]+(?=\d)", "", info)
    expected_number_tokens = set(re.findall(r"\d{6,}", collapsed_numeric_info))
    if expected_number_tokens:
        vision_digits = re.sub(r"[^\d]", "", vision_text)
        if any(token in vision_digits for token in expected_number_tokens):
            return {"ok": True}
        return {"ok": False, "message": "El comprobante no coincide con el número o cuenta configurada para este método de pago."}

    name_like_segments = [
        normalize_catalog_text(segment)
        for segment in re.split(r"[\n,;|]+", info)
        if len(segment.strip()) >= 5 and not any(ch.isdigit() for ch in segment)
    ]
    name_like_segments = [
        segment
        for segment in name_like_segments
        if segment and segment not in {"nombre", "correo", "telefono", "instrucciones", "pago"}
    ]
    if name_like_segments and any(segment in vision_text for segment in name_like_segments):
        return {"ok": True}

    normalized_info = normalize_catalog_text(info)
    if normalized_info and normalized_info in vision_text:
        return {"ok": True}

    return {"ok": False, "message": "El comprobante no coincide con los datos del método de pago configurado."}


def safe_decimal(value) -> Decimal | None:
    """Extract a Decimal amount from user- or vision-supplied text."""
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return _decimal_amount(match.group(0))
    except InvalidOperation:
        return None


def safe_float(value) -> float | None:
    amount = safe_decimal(value)
    return float(amount) if amount is not None else None


def _decimal_amount(value) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _result(status, *, order_id=None, expected_amount=None, detected_amount=None, message="", payment_method=None):
    context = {
        "status": status,
        "message": message,
    }
    if payment_method:
        context["payment_method"] = payment_method
    return PaymentVerificationResult(
        status=status,
        order_id=order_id,
        expected_amount=expected_amount,
        detected_amount=detected_amount,
        customer_message_context=context,
    )


async def _mark_session_payment_verified(customer_id: str, order_id: str) -> None:
    await sessions.set_current_order(customer_id, order_id, workflow_stage="completed", active_agent="payment")


async def _get_payment_target_order(customer_id: str) -> dict | None:
    """Prefer the order explicitly associated with the active workflow."""
    try:
        session = await sessions.get_session(customer_id)
    except Exception as exc:
        logger.warning("Could not read payment workflow session; falling back to latest open order: %s", exc)
        session = None
    if session and session.current_order_id:
        order = await orders.get_customer_open_order_by_id(customer_id, session.current_order_id)
        if order:
            return order
    return await orders.get_latest_open_order(customer_id)
