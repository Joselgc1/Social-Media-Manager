"""Deterministic payment-proof verifier."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app import db
from app.ai.payment.models import PaymentVerificationResult
from app.ai.tools.catalog import normalize_catalog_text
from app.crm import orders, sessions
from app.exchange_rates import parse_decimal, selected_exchange_rate

AMOUNT_TOLERANCE = Decimal("0.01")
MAX_PROOF_AGE = timedelta(days=7)
MAX_VES_PROOF_AGE = timedelta(days=3)
MAX_FUTURE_SKEW = timedelta(minutes=10)
ORDER_DATE_GRACE = timedelta(days=1)
METHOD_CURRENCIES = {
    "zelle": "USD",
    "zinli": "USD",
    "binance": "USDT",
    "bank_transfer": "VES",
}
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
    order, target_status = await _get_payment_target_order(customer_id)
    if target_status == "ambiguous":
        return _result(
            "ambiguous_order",
            message="Hay varios pedidos abiertos y el comprobante no está vinculado a uno específico.",
        )
    if not order:
        return _result("no_open_order", message="No existe un pedido abierto para validar este comprobante.")

    order_id = str(order.get("id") or order.get("order_id") or "")
    expected_amount = _decimal_amount(order.get("total"))
    expected_method_name = str(order.get("payment_method", "") or "").strip()

    if str(order.get("payment_status") or "").strip().lower() in {"proof_received", "confirmed"}:
        if update_order:
            await _mark_session_payment_verified(customer_id)
        return _result(
            "verified",
            order_id=order_id,
            expected_amount=expected_amount,
            message="El comprobante de este pedido ya fue recibido.",
            payment_method=expected_method_name,
        )

    if not vision_result.get("analyzed"):
        return _result("unreadable", order_id=order_id, expected_amount=expected_amount, message="No se pudo analizar el comprobante.")

    confidence = str(vision_result.get("confidence") or "").strip().lower()
    if confidence != "high":
        return _result(
            "low_confidence",
            order_id=order_id,
            expected_amount=expected_amount,
            message="El comprobante no tiene suficiente confianza para validarse automáticamente.",
        )

    transaction_at = parse_transaction_date(vision_result.get("date"))
    if not _is_transaction_date_valid(transaction_at, order.get("created_at")):
        return _result(
            "invalid_date",
            order_id=order_id,
            expected_amount=expected_amount,
            message="La fecha del comprobante no es válida o no corresponde a este pedido.",
        )

    reference = str(vision_result.get("reference") or "").strip()
    normalized_reference = normalize_payment_reference(reference)
    proof_hash = str(vision_result.get("proof_hash") or "").strip().lower()
    if len(normalized_reference) < 4 or not re.fullmatch(r"[a-f0-9]{64}", proof_hash):
        return _result(
            "manual_review",
            order_id=order_id,
            expected_amount=expected_amount,
            message="El comprobante no incluye una referencia verificable.",
        )

    payment_method = find_payment_method(payment_methods, expected_method_name)
    if not payment_method:
        return _result(
            "manual_review",
            order_id=order_id,
            expected_amount=expected_amount,
            message="No se encontró la configuración del método de pago del pedido.",
        )

    expected_kind = classify_payment_method_name(expected_method_name)
    detected_kind = str(vision_result.get("payment_method", "") or "").strip().lower()
    if not expected_kind or detected_kind != expected_kind:
        return _result(
            "method_mismatch",
            order_id=order_id,
            expected_amount=expected_amount,
            message="El comprobante parece ser de un método distinto al pedido.",
        )

    expected_currency = METHOD_CURRENCIES.get(expected_kind or "")
    detected_currency = normalize_currency(vision_result.get("currency"))
    if not expected_currency or detected_currency != expected_currency:
        return _result(
            "currency_mismatch",
            order_id=order_id,
            expected_amount=expected_amount,
            expected_currency=expected_currency,
            detected_currency=detected_currency,
            message="La moneda del comprobante no coincide con el método de pago del pedido.",
        )

    if expected_currency == "VES" and not _is_recent_transaction(transaction_at, MAX_VES_PROOF_AGE):
        return _result(
            "invalid_date",
            order_id=order_id,
            expected_amount=expected_amount,
            expected_currency=expected_currency,
            detected_currency=detected_currency,
            message="La fecha del comprobante en bolívares es demasiado antigua para aplicar la tasa actual.",
        )

    expected_receipt_amount = await _expected_receipt_amount(
        expected_amount,
        order_currency=str(order.get("currency") or "USD"),
        receipt_currency=expected_currency,
    )
    if expected_receipt_amount is None:
        return _result(
            "manual_review",
            order_id=order_id,
            expected_amount=expected_amount,
            expected_currency=expected_currency,
            detected_currency=detected_currency,
            message="No hay una tasa válida disponible para verificar este comprobante.",
        )

    detected_amount = safe_decimal(vision_result.get("amount"))
    if detected_amount is None:
        return _result(
            "unreadable",
            order_id=order_id,
            expected_amount=expected_receipt_amount,
            expected_currency=expected_currency,
            detected_currency=detected_currency,
            message="No se pudo leer el monto del comprobante.",
        )

    if abs(detected_amount - expected_receipt_amount) > AMOUNT_TOLERANCE:
        return _result(
            "amount_mismatch",
            order_id=order_id,
            expected_amount=expected_receipt_amount,
            detected_amount=detected_amount,
            expected_currency=expected_currency,
            detected_currency=detected_currency,
            message="El comprobante no coincide con el monto esperado del pedido.",
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
        reference_key = hashlib.sha256(
            (
                f"{normalize_catalog_text(expected_method_name)}|{expected_kind}|"
                f"{expected_currency}|{normalized_reference}"
            ).encode()
        ).hexdigest()
        updated = await orders.update_order_payment_status(
            order_id,
            status="proof_received",
            note=confirmation_note,
            proof_metadata={
                "proof_hash": proof_hash,
                "reference": reference,
                "reference_key": reference_key,
                "currency": detected_currency,
                "amount": detected_amount,
                "transaction_at": transaction_at,
            },
        )
        if not updated:
            return _result(
                "manual_review",
                order_id=order_id,
                expected_amount=expected_amount,
                detected_amount=detected_amount,
                message="No se pudo actualizar el estado del pedido.",
            )
        if updated.get("payment_status") == "replay_detected":
            return _result(
                "duplicate_proof",
                order_id=order_id,
                expected_amount=expected_receipt_amount,
                detected_amount=detected_amount,
                expected_currency=expected_currency,
                detected_currency=detected_currency,
                message="Este comprobante ya fue usado para otro pedido.",
            )
        await _mark_session_payment_verified(customer_id)

    return _result(
        "verified",
        order_id=order_id,
        expected_amount=expected_receipt_amount,
        detected_amount=detected_amount,
        expected_currency=expected_currency,
        detected_currency=detected_currency,
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

    recipient_values = [
        str(vision_result.get(key, "") or "")
        for key in ("recipient_identifier", "recipient_name")
    ]
    expected_emails = {
        email.lower()
        for email in re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", info, flags=re.IGNORECASE)
    }
    if expected_emails:
        detected_emails = {
            email.lower()
            for value in recipient_values
            for email in re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", value, flags=re.IGNORECASE)
        }
        if expected_emails & detected_emails:
            return {"ok": True}
        return {"ok": False, "message": "El comprobante no muestra el correo o destinatario configurado para este método de pago."}

    collapsed_numeric_info = re.sub(r"(?<=\d)[\s\-]+(?=\d)", "", info)
    expected_number_tokens = set(re.findall(r"\d{6,}", collapsed_numeric_info))
    if expected_number_tokens:
        detected_number_tokens = {
            token
            for value in recipient_values
            for token in re.findall(r"\d{6,}", re.sub(r"(?<=\d)[\s\-]+(?=\d)", "", value))
        }
        if expected_number_tokens & detected_number_tokens:
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
    normalized_recipient_values = {normalize_catalog_text(value) for value in recipient_values}
    if name_like_segments and any(segment in normalized_recipient_values for segment in name_like_segments):
        return {"ok": True}

    return {"ok": False, "message": "El comprobante no coincide con los datos del método de pago configurado."}


def safe_decimal(value) -> Decimal | None:
    """Extract a Decimal amount from user- or vision-supplied text."""
    amount = parse_decimal(value)
    if amount is None or amount < 0:
        return None
    try:
        return _decimal_amount(amount)
    except InvalidOperation:
        return None


def safe_float(value) -> float | None:
    amount = safe_decimal(value)
    return float(amount) if amount is not None else None


def normalize_currency(value) -> str | None:
    text = normalize_catalog_text(str(value or "")).upper()
    aliases = {
        "$": "USD",
        "DOLLAR": "USD",
        "DOLLARS": "USD",
        "DOLAR": "USD",
        "DOLARES": "USD",
        "BS": "VES",
        "BS.": "VES",
        "BOLIVAR": "VES",
        "BOLIVARES": "VES",
    }
    normalized = aliases.get(text, text)
    return normalized if normalized in {"USD", "VES", "USDT"} else None


def normalize_payment_reference(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize_catalog_text(value))


def parse_transaction_date(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for date_format in ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(text, date_format)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_transaction_date_valid(transaction_at: datetime | None, order_created_at) -> bool:
    if transaction_at is None:
        return False
    now = datetime.now(UTC)
    if transaction_at > now + MAX_FUTURE_SKEW or transaction_at < now - MAX_PROOF_AGE:
        return False
    order_date = parse_transaction_date(order_created_at)
    return not order_date or transaction_at >= order_date - ORDER_DATE_GRACE


def _is_recent_transaction(transaction_at: datetime, max_age: timedelta) -> bool:
    return transaction_at >= datetime.now(UTC) - max_age


async def _expected_receipt_amount(
    order_amount: Decimal | None,
    *,
    order_currency: str,
    receipt_currency: str,
) -> Decimal | None:
    if order_amount is None or order_currency.upper() != "USD":
        return None
    if receipt_currency in {"USD", "USDT"}:
        return order_amount
    if receipt_currency != "VES":
        return None
    selected = selected_exchange_rate(await db.get_settings())
    rate = selected.get("rate")
    if rate is None or rate <= 0:
        return None
    return (order_amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _decimal_amount(value) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _result(
    status,
    *,
    order_id=None,
    expected_amount=None,
    detected_amount=None,
    expected_currency=None,
    detected_currency=None,
    message="",
    payment_method=None,
):
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
        expected_currency=expected_currency,
        detected_currency=detected_currency,
        customer_message_context=context,
    )


async def _mark_session_payment_verified(customer_id: str) -> None:
    await sessions.set_current_order(customer_id, None, workflow_stage="completed", active_agent="payment")


async def _get_payment_target_order(customer_id: str) -> tuple[dict | None, str]:
    """Use an explicit session binding, or a sole unambiguous open order."""
    try:
        session = await sessions.get_session(customer_id)
    except Exception as exc:
        logger.warning("Could not read payment workflow session; falling back to latest open order: %s", exc)
        session = None
    if (
        session
        and session.current_order_id
        and session.workflow_stage == "waiting_for_payment"
    ):
        order = await orders.get_customer_open_order_by_id(customer_id, session.current_order_id)
        if order:
            return order, "session"
    order, ambiguous = await orders.get_unambiguous_open_order(customer_id)
    if ambiguous:
        return None, "ambiguous"
    return order, "single" if order else "none"
