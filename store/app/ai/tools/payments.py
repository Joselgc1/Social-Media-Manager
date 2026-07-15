"""
Payment-status tool handlers and compatibility validation helpers.
"""

from __future__ import annotations

from app.ai.payment import verifier as payment_verifier
from app.ai.tools.context import ToolExecutionContext


async def update_payment_status(args: dict, context: ToolExecutionContext) -> dict:
    """Update payment status, validating screenshot evidence when available."""
    customer_id = context.customer["id"]
    if context.payment_proof_attempt:
        result = await payment_verifier.verify_payment_proof(
            customer_id=customer_id,
            payment_methods=context.payment_methods,
            vision_result=context.vision_result or {},
            confirmation_note=args.get("confirmation_note"),
            update_order=True,
        )
        return _tool_result_from_verification(result)

    return {
        "status": "error",
        "message": "Payment status can only be updated after deterministic payment-proof validation.",
    }


async def validate_payment_proof(customer_id: str, payment_methods: list[dict], vision_result: dict) -> dict:
    """Validate a payment screenshot against the customer's latest open order."""
    result = await payment_verifier.verify_payment_proof(
        customer_id=customer_id,
        payment_methods=payment_methods,
        vision_result=vision_result,
        update_order=False,
    )
    return _validation_dict_from_verification(result)


def _tool_result_from_verification(result) -> dict:
    if result.verified:
        return {
            "order_id": result.order_id,
            "payment_status": "proof_received",
            "validated_amount": payment_verifier.safe_float(result.expected_amount),
            "validated_payment_method": result.customer_message_context.get("payment_method"),
        }
    return {
        "status": "error",
        "message": result.customer_message_context.get("message") or "No se pudo validar el comprobante de pago.",
        "verification_status": result.status,
        "order_id": result.order_id,
    }


def _validation_dict_from_verification(result) -> dict:
    if result.verified:
        return {
            "status": "ok",
            "order_id": result.order_id,
            "validated_amount": payment_verifier.safe_float(result.expected_amount),
            "validated_payment_method": result.customer_message_context.get("payment_method"),
        }
    return {
        "status": "error",
        "message": result.customer_message_context.get("message") or "No se pudo validar el comprobante de pago.",
        "verification_status": result.status,
        "order_id": result.order_id,
    }


def find_payment_method(payment_methods: list[dict], method_name: str) -> dict | None:
    return payment_verifier.find_payment_method(payment_methods, method_name)


def classify_payment_method_name(method_name: str) -> str | None:
    return payment_verifier.classify_payment_method_name(method_name)


def validate_payment_identifier_match(payment_information: str, vision_result: dict) -> dict:
    return payment_verifier.validate_payment_identifier_match(payment_information, vision_result)


def safe_float(value) -> float | None:
    return payment_verifier.safe_float(value)
