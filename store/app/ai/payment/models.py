"""Typed payment verification results."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

PaymentVerificationStatus = Literal[
    "verified",
    "no_open_order",
    "unreadable",
    "amount_mismatch",
    "method_mismatch",
    "recipient_mismatch",
    "not_completed",
    "manual_review",
]


@dataclass(slots=True)
class PaymentVerificationResult:
    status: PaymentVerificationStatus
    order_id: str | None = None
    expected_amount: Decimal | None = None
    detected_amount: Decimal | None = None
    customer_message_context: dict = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.status == "verified"
