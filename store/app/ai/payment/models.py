"""Typed payment verification results."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

PaymentVerificationStatus = Literal[
    "verified",
    "no_open_order",
    "ambiguous_order",
    "unreadable",
    "amount_mismatch",
    "currency_mismatch",
    "method_mismatch",
    "recipient_mismatch",
    "not_completed",
    "low_confidence",
    "invalid_date",
    "duplicate_proof",
    "manual_review",
]


@dataclass(slots=True)
class PaymentVerificationResult:
    status: PaymentVerificationStatus
    order_id: str | None = None
    expected_amount: Decimal | None = None
    detected_amount: Decimal | None = None
    expected_currency: str | None = None
    detected_currency: str | None = None
    customer_message_context: dict = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.status == "verified"
