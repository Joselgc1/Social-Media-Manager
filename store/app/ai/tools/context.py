"""
Server-owned context passed to tool handlers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolExecutionContext:
    """Runtime data owned by the server and unavailable to the model."""

    customer: dict[str, Any]
    channel: str
    payment_methods: list[dict[str, Any]] = field(default_factory=list)
    vision_result: dict[str, Any] | None = None
    payment_proof_attempt: bool = False
    latest_user_message: str = ""
    session: Any | None = None
