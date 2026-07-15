"""
Persistent conversation workflow state.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app import db

logger = logging.getLogger(__name__)

VALID_ACTIVE_AGENTS = {"legacy", "sales", "checkout", "payment", "support"}
VALID_WORKFLOW_STAGES = {
    "idle",
    "sales",
    "checkout_collecting",
    "checkout_ready",
    "waiting_for_payment",
    "support",
    "completed",
    "cancelled",
}
VALID_SHIPPING_METHODS = {"mrw", "zoom"}


class CheckoutDraftItem(BaseModel):
    """Server-owned draft item. Prices are intentionally not persisted here."""

    model_config = ConfigDict(extra="ignore")

    product_query: str | None = None
    sku: str | None = None
    parent_sku: str | None = None
    product_name: str | None = None
    canonical_sku: str | None = None
    size: str | None = None
    quantity: int | None = None

    @field_validator("product_query", "sku", "parent_sku", "product_name", "canonical_sku", mode="before")
    @classmethod
    def _clean_text(cls, value):
        text = str(value or "").strip()
        return text or None

    @field_validator("size", mode="before")
    @classmethod
    def _clean_size(cls, value):
        text = str(value or "").strip().upper()
        return text or None

    @field_validator("quantity", mode="before")
    @classmethod
    def _clean_quantity(cls, value):
        try:
            quantity = int(value)
        except (TypeError, ValueError):
            return None
        return quantity if quantity > 0 else None


class CheckoutDraft(BaseModel):
    """Typed checkout draft stored in conversation_sessions.checkout_draft."""

    model_config = ConfigDict(extra="ignore")

    items: list[CheckoutDraftItem] = Field(default_factory=list)
    shipping_method: str | None = None
    shipping_city: str | None = None
    shipping_address: str | None = None
    payment_method: str | None = None

    @field_validator("items", mode="before")
    @classmethod
    def _clean_items(cls, value):
        return value if isinstance(value, list) else []

    @field_validator("shipping_method", mode="before")
    @classmethod
    def _clean_shipping_method(cls, value):
        text = str(value or "").strip().lower()
        return text if text in VALID_SHIPPING_METHODS else None

    @field_validator("shipping_city", "shipping_address", "payment_method", mode="before")
    @classmethod
    def _clean_optional_text(cls, value):
        text = str(value or "").strip()
        return text or None

    def missing_fields(self) -> list[str]:
        missing = []
        if not self.items:
            missing.append("items")
        else:
            for index, item in enumerate(self.items):
                prefix = f"items[{index}]"
                if not (item.canonical_sku or item.sku or item.product_query or item.product_name):
                    missing.append(f"{prefix}.product")
                if not item.size:
                    missing.append(f"{prefix}.size")
                if not item.quantity:
                    missing.append(f"{prefix}.quantity")
        if not self.shipping_method:
            missing.append("shipping_method")
        if not self.shipping_city:
            missing.append("shipping_city")
        if not self.shipping_address:
            missing.append("shipping_address")
        if not self.payment_method:
            missing.append("payment_method")
        return missing

    def public_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True, exclude_defaults=True)


class ConversationSession(BaseModel):
    """Typed view of the active workflow session for one customer."""

    model_config = ConfigDict(extra="ignore")

    customer_id: str
    active_agent: str = "legacy"
    active_intent: str | None = None
    workflow_stage: str = "idle"
    checkout_draft: CheckoutDraft = Field(default_factory=CheckoutDraft)
    current_order_id: str | None = None
    last_route_confidence: float | None = None
    created_at: Any = None
    updated_at: Any = None

    @field_validator("active_agent", mode="before")
    @classmethod
    def _normalize_active_agent(cls, value):
        text = str(value or "legacy").strip().lower()
        return text if text in VALID_ACTIVE_AGENTS else "legacy"

    @field_validator("workflow_stage", mode="before")
    @classmethod
    def _normalize_workflow_stage(cls, value):
        text = str(value or "idle").strip().lower()
        return text if text in VALID_WORKFLOW_STAGES else "idle"

    @field_validator("active_intent", "current_order_id", mode="before")
    @classmethod
    def _normalize_optional_text(cls, value):
        text = str(value or "").strip()
        return text or None

    @field_validator("last_route_confidence", mode="before")
    @classmethod
    def _normalize_confidence(cls, value):
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(confidence, 1.0))

    @field_validator("checkout_draft", mode="before")
    @classmethod
    def _normalize_checkout_draft(cls, value):
        return normalize_checkout_draft(value)

    def workflow_context(self) -> dict[str, Any]:
        return {
            "active_agent": self.active_agent,
            "active_intent": self.active_intent,
            "workflow_stage": self.workflow_stage,
            "checkout_draft": self.checkout_draft.public_dict(),
            "current_order_id": self.current_order_id,
            "last_route_confidence": self.last_route_confidence,
        }


def normalize_checkout_draft(raw_value: Any) -> CheckoutDraft:
    """Safely coerce DB JSON/strings into a checkout draft."""
    if isinstance(raw_value, CheckoutDraft):
        return raw_value
    if isinstance(raw_value, str):
        try:
            raw_value = json.loads(raw_value or "{}")
        except json.JSONDecodeError:
            raw_value = {}
    if not isinstance(raw_value, dict):
        raw_value = {}
    try:
        return CheckoutDraft.model_validate(raw_value)
    except ValidationError:
        return CheckoutDraft()


def merge_checkout_draft(existing: CheckoutDraft | dict | str | None, partial_update: dict | None) -> CheckoutDraft:
    """Merge partial model-supplied fields into the server draft."""
    draft_data = normalize_checkout_draft(existing).public_dict()
    update = partial_update if isinstance(partial_update, dict) else {}

    if isinstance(update.get("items"), list):
        draft_data["items"] = update["items"]
    else:
        item_keys = {"product_query", "sku", "parent_sku", "product_name", "canonical_sku", "size", "quantity"}
        item_patch = {key: update[key] for key in item_keys if key in update}
        if item_patch:
            items = list(draft_data.get("items") or [{}])
            first_item = dict(items[0] or {})
            first_item.update(item_patch)
            items[0] = first_item
            draft_data["items"] = items

    for key in ("shipping_method", "shipping_city", "shipping_address", "payment_method"):
        if key in update:
            draft_data[key] = update[key]

    return normalize_checkout_draft(draft_data)


async def get_session(customer_id: str) -> ConversationSession | None:
    row = await db.fetch_one(
        """
        SELECT customer_id, active_agent, active_intent, workflow_stage, checkout_draft,
               current_order_id, last_route_confidence, created_at, updated_at
        FROM conversation_sessions
        WHERE customer_id = :customer_id
        """,
        {"customer_id": customer_id},
    )
    return _row_to_session(row) if row else None


async def get_or_create_session(customer_id: str) -> ConversationSession:
    row = await db.fetch_one(
        """
        INSERT INTO conversation_sessions (customer_id)
        VALUES (:customer_id)
        ON CONFLICT (customer_id) DO UPDATE
        SET updated_at = conversation_sessions.updated_at
        RETURNING customer_id, active_agent, active_intent, workflow_stage, checkout_draft,
                  current_order_id, last_route_confidence, created_at, updated_at
        """,
        {"customer_id": customer_id},
    )
    if row:
        return _row_to_session(row)
    fallback = await get_session(customer_id)
    return fallback or ConversationSession(customer_id=customer_id)


async def set_active_agent(
    customer_id: str,
    active_agent: str,
    *,
    active_intent: str | None = None,
    workflow_stage: str | None = None,
    last_route_confidence: float | None = None,
) -> ConversationSession:
    normalized_agent = ConversationSession(customer_id=customer_id, active_agent=active_agent).active_agent
    normalized_stage = None
    if workflow_stage is not None:
        normalized_stage = ConversationSession(customer_id=customer_id, workflow_stage=workflow_stage).workflow_stage
    row = await db.fetch_one(
        """
        INSERT INTO conversation_sessions (customer_id, active_agent, active_intent, workflow_stage, last_route_confidence)
        VALUES (:customer_id, :active_agent, :active_intent, COALESCE(:workflow_stage, 'idle'), :last_route_confidence)
        ON CONFLICT (customer_id) DO UPDATE
        SET active_agent = :active_agent,
            active_intent = :active_intent,
            workflow_stage = COALESCE(:workflow_stage, conversation_sessions.workflow_stage),
            last_route_confidence = :last_route_confidence,
            updated_at = NOW()
        RETURNING customer_id, active_agent, active_intent, workflow_stage, checkout_draft,
                  current_order_id, last_route_confidence, created_at, updated_at
        """,
        {
            "customer_id": customer_id,
            "active_agent": normalized_agent,
            "active_intent": (active_intent or "").strip() or None,
            "workflow_stage": normalized_stage,
            "last_route_confidence": last_route_confidence,
        },
    )
    logger.info(
        "AI workflow transition",
        extra={"active_agent": normalized_agent, "workflow_stage": normalized_stage, "active_intent": active_intent},
    )
    return _row_to_session(row)


async def set_workflow_stage(customer_id: str, workflow_stage: str) -> ConversationSession:
    normalized_stage = ConversationSession(customer_id=customer_id, workflow_stage=workflow_stage).workflow_stage
    row = await db.fetch_one(
        """
        UPDATE conversation_sessions
        SET workflow_stage = :workflow_stage, updated_at = NOW()
        WHERE customer_id = :customer_id
        RETURNING customer_id, active_agent, active_intent, workflow_stage, checkout_draft,
                  current_order_id, last_route_confidence, created_at, updated_at
        """,
        {"customer_id": customer_id, "workflow_stage": normalized_stage},
    )
    logger.info("AI workflow stage updated", extra={"workflow_stage": normalized_stage})
    return _row_to_session(row) if row else await set_active_agent(customer_id, "legacy", workflow_stage=normalized_stage)


async def update_checkout_draft(customer_id: str, partial_update: dict) -> ConversationSession:
    session = await get_or_create_session(customer_id)
    draft = merge_checkout_draft(session.checkout_draft, partial_update)
    row = await db.fetch_one(
        """
        UPDATE conversation_sessions
        SET checkout_draft = CAST(:checkout_draft AS jsonb),
            active_agent = 'checkout',
            workflow_stage = :workflow_stage,
            updated_at = NOW()
        WHERE customer_id = :customer_id
        RETURNING customer_id, active_agent, active_intent, workflow_stage, checkout_draft,
                  current_order_id, last_route_confidence, created_at, updated_at
        """,
        {
            "customer_id": customer_id,
            "checkout_draft": json.dumps(draft.public_dict(), ensure_ascii=False),
            "workflow_stage": "checkout_ready" if not draft.missing_fields() else "checkout_collecting",
        },
    )
    logger.info(
        "AI checkout draft updated",
        extra={"workflow_stage": "checkout_ready" if not draft.missing_fields() else "checkout_collecting"},
    )
    return _row_to_session(row)


async def set_current_order(
    customer_id: str,
    order_id: str | None,
    *,
    workflow_stage: str = "waiting_for_payment",
    active_agent: str = "checkout",
) -> ConversationSession:
    normalized_agent = ConversationSession(customer_id=customer_id, active_agent=active_agent).active_agent
    normalized_stage = ConversationSession(customer_id=customer_id, workflow_stage=workflow_stage).workflow_stage
    row = await db.fetch_one(
        """
        INSERT INTO conversation_sessions (customer_id, active_agent, workflow_stage, current_order_id)
        VALUES (:customer_id, :active_agent, :workflow_stage, :order_id)
        ON CONFLICT (customer_id) DO UPDATE
        SET active_agent = :active_agent,
            workflow_stage = :workflow_stage,
            current_order_id = :order_id,
            checkout_draft = '{}'::jsonb,
            updated_at = NOW()
        RETURNING customer_id, active_agent, active_intent, workflow_stage, checkout_draft,
                  current_order_id, last_route_confidence, created_at, updated_at
        """,
        {
            "customer_id": customer_id,
            "active_agent": normalized_agent,
            "order_id": order_id,
            "workflow_stage": normalized_stage,
        },
    )
    logger.info(
        "AI current order transition",
        extra={"active_agent": normalized_agent, "workflow_stage": normalized_stage, "has_order": bool(order_id)},
    )
    return _row_to_session(row)


async def reset_session(customer_id: str) -> ConversationSession:
    row = await db.fetch_one(
        """
        INSERT INTO conversation_sessions (customer_id)
        VALUES (:customer_id)
        ON CONFLICT (customer_id) DO UPDATE
        SET active_agent = 'legacy',
            active_intent = NULL,
            workflow_stage = 'idle',
            checkout_draft = '{}'::jsonb,
            current_order_id = NULL,
            last_route_confidence = NULL,
            updated_at = NOW()
        RETURNING customer_id, active_agent, active_intent, workflow_stage, checkout_draft,
                  current_order_id, last_route_confidence, created_at, updated_at
        """,
        {"customer_id": customer_id},
    )
    logger.info("AI workflow reset", extra={"active_agent": "legacy", "workflow_stage": "idle"})
    return _row_to_session(row)


def _row_to_session(row) -> ConversationSession:
    data = dict(row)
    data["customer_id"] = str(data.get("customer_id") or "")
    data["current_order_id"] = str(data["current_order_id"]) if data.get("current_order_id") else None
    try:
        return ConversationSession.model_validate(data)
    except ValidationError:
        return ConversationSession(customer_id=data["customer_id"])
