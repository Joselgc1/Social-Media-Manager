"""
Persistent conversation workflow state.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

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
VALID_FULFILLMENT_TYPES = {"home_delivery", "courier_agency_pickup"}


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
    fulfillment_type: str | None = None
    shipping_zone: str | None = None
    pickup_agency: str | None = None
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

    @field_validator("fulfillment_type", mode="before")
    @classmethod
    def _clean_fulfillment_type(cls, value):
        text = str(value or "").strip().lower()
        return text if text in VALID_FULFILLMENT_TYPES else None

    @field_validator("shipping_city", "shipping_address", "shipping_zone", "pickup_agency", "payment_method", mode="before")
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
        if not self.shipping_city:
            missing.append("shipping_city")
        if self.fulfillment_type == "home_delivery":
            if not self.shipping_zone:
                missing.append("shipping_zone")
            if not self.shipping_address:
                missing.append("shipping_address")
        elif self.fulfillment_type == "courier_agency_pickup":
            if not self.shipping_method:
                missing.append("shipping_method")
            if not self.pickup_agency:
                missing.append("pickup_agency")
        else:
            # Preserve legacy drafts until the delivery policy resolves their city.
            if not self.shipping_method:
                missing.append("shipping_method")
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


    @field_validator("customer_id", mode="before")
    @classmethod
    def _normalize_customer_id(cls, value):
        return _customer_id_text(value)

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


class InstagramContentContext(BaseModel):
    """Short-lived identifiers for a private Instagram Story conversation."""

    model_config = ConfigDict(extra="ignore")

    source: str
    content_id: str | None = None
    story_id: str
    product_skus: list[str] = Field(default_factory=list, max_length=20)
    selected_product_sku: str | None = None
    mapping_status: str = "resolved"
    meta_context_event_id: str | None = None

    @field_validator("source")
    @classmethod
    def _story_source(cls, value):
        if str(value or "").strip() != "story_reply":
            raise ValueError("Unsupported Instagram content context source")
        return "story_reply"

    @field_validator("story_id", "content_id", "selected_product_sku", "meta_context_event_id", mode="before")
    @classmethod
    def _clean_context_text(cls, value):
        text = str(value or "").strip()
        return text or None

    @field_validator("product_skus", mode="before")
    @classmethod
    def _clean_context_skus(cls, value):
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))[:20]


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

    for key in (
        "shipping_method",
        "shipping_city",
        "shipping_address",
        "fulfillment_type",
        "shipping_zone",
        "pickup_agency",
        "payment_method",
    ):
        if key in update:
            draft_data[key] = update[key]

    return normalize_checkout_draft(draft_data)


async def get_session(customer_id: str) -> ConversationSession | None:
    customer_id = _customer_id_text(customer_id)
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
    customer_id = _customer_id_text(customer_id)
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
    customer_id = _customer_id_text(customer_id)
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
    customer_id = _customer_id_text(customer_id)
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
    customer_id = _customer_id_text(customer_id)
    await get_or_create_session(customer_id)
    async with db.get_db().transaction():
        current = await db.fetch_one(
            """
            SELECT checkout_draft
            FROM conversation_sessions
            WHERE customer_id = :customer_id
            FOR UPDATE
            """,
            {"customer_id": customer_id},
        )
        draft = merge_checkout_draft(current["checkout_draft"] if current else None, partial_update)
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
    customer_id = _customer_id_text(customer_id)
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
    customer_id = _customer_id_text(customer_id)
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
            instagram_content_context = '{}'::jsonb,
            instagram_context_expires_at = NULL,
            updated_at = NOW()
        RETURNING customer_id, active_agent, active_intent, workflow_stage, checkout_draft,
                  current_order_id, last_route_confidence, created_at, updated_at
        """,
        {"customer_id": customer_id},
    )
    logger.info("AI workflow reset", extra={"active_agent": "legacy", "workflow_stage": "idle"})
    return _row_to_session(row)


async def store_instagram_content_context(
    customer_id: str,
    context: dict,
    *,
    ttl_hours: int,
) -> dict:
    """Replace the customer's private Story context with validated identifiers."""
    customer_id = _customer_id_text(customer_id)
    normalized = InstagramContentContext.model_validate(context).model_dump(exclude_none=True)
    if normalized.get("mapping_status") != "resolved" or not normalized.get("product_skus"):
        await clear_instagram_content_context(customer_id)
        return {}
    selected = normalized.get("selected_product_sku")
    if selected and selected not in normalized["product_skus"]:
        normalized.pop("selected_product_sku", None)
    await db.execute(
        """
        INSERT INTO conversation_sessions (
            customer_id, instagram_content_context, instagram_context_expires_at
        ) VALUES (
            :customer_id, CAST(:context AS jsonb), NOW() + (:ttl_hours * INTERVAL '1 hour')
        )
        ON CONFLICT (customer_id) DO UPDATE
        SET instagram_content_context = CAST(:context AS jsonb),
            instagram_context_expires_at = NOW() + (:ttl_hours * INTERVAL '1 hour'),
            updated_at = NOW()
        """,
        {
            "customer_id": customer_id,
            "context": json.dumps(normalized, ensure_ascii=False),
            "ttl_hours": ttl_hours,
        },
    )
    return normalized


async def load_active_instagram_content_context(customer_id: str) -> dict:
    """Load active Story context and atomically clear expired or invalid data."""
    customer_id = _customer_id_text(customer_id)
    row = await db.fetch_one(
        """
        SELECT instagram_content_context, instagram_context_expires_at
        FROM conversation_sessions
        WHERE customer_id = :customer_id
        """,
        {"customer_id": customer_id},
    )
    if not row:
        return {}
    expires_at = row["instagram_context_expires_at"]
    if not expires_at or _context_datetime(expires_at) <= datetime.now(UTC):
        await clear_instagram_content_context(customer_id)
        return {}
    raw_context = row["instagram_content_context"]
    if isinstance(raw_context, str):
        try:
            raw_context = json.loads(raw_context)
        except json.JSONDecodeError:
            raw_context = {}
    try:
        context = InstagramContentContext.model_validate(raw_context).model_dump(exclude_none=True)
    except ValidationError:
        await clear_instagram_content_context(customer_id)
        return {}
    if context.get("mapping_status") != "resolved" or not context.get("product_skus"):
        await clear_instagram_content_context(customer_id)
        return {}
    content_id = context.get("content_id")
    if content_id:
        try:
            normalized_content_id = str(UUID(content_id))
        except ValueError:
            await clear_instagram_content_context(customer_id)
            return {}
        mappings = await db.fetch_all(
            """
            SELECT mapping.product_sku
            FROM instagram_content content
            JOIN instagram_content_products mapping ON mapping.content_id = content.id
            WHERE content.id = :content_id
              AND content.content_type = 'story'
              AND content.status = 'active'
              AND content.media_id = :story_id
            ORDER BY mapping.display_order, mapping.product_sku
            """,
            {"content_id": normalized_content_id, "story_id": context["story_id"]},
        )
        active_skus = {str(mapping["product_sku"]).strip() for mapping in mappings}
        if not active_skus or not set(context["product_skus"]).issubset(active_skus):
            await clear_instagram_content_context(customer_id)
            return {}
    return context


async def clear_instagram_content_context(customer_id: str) -> None:
    await db.execute(
        """
        UPDATE conversation_sessions
        SET instagram_content_context = '{}'::jsonb,
            instagram_context_expires_at = NULL,
            updated_at = NOW()
        WHERE customer_id = :customer_id
        """,
        {"customer_id": _customer_id_text(customer_id)},
    )


async def update_instagram_selected_product(
    customer_id: str,
    selected_product_sku: str,
) -> dict:
    """Persist one explicit selection only while it remains in the active Story mapping."""
    row = await db.fetch_one(
        """
        UPDATE conversation_sessions session
        SET instagram_content_context = jsonb_set(
                session.instagram_content_context,
                '{selected_product_sku}',
                to_jsonb(CAST(:selected_product_sku AS text)),
                true
            ),
            updated_at = NOW()
        WHERE session.customer_id = :customer_id
          AND session.instagram_context_expires_at > NOW()
          AND EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(
                  session.instagram_content_context -> 'product_skus'
              ) sku
              WHERE sku = :selected_product_sku
          )
          AND EXISTS (
              SELECT 1
              FROM instagram_content content
              JOIN instagram_content_products mapping ON mapping.content_id = content.id
              WHERE content.id::text = session.instagram_content_context ->> 'content_id'
                AND content.content_type = 'story'
                AND content.status = 'active'
                AND content.media_id = session.instagram_content_context ->> 'story_id'
                AND mapping.product_sku = :selected_product_sku
          )
        RETURNING instagram_content_context
        """,
        {
            "customer_id": _customer_id_text(customer_id),
            "selected_product_sku": str(selected_product_sku or "").strip(),
        },
    )
    if not row:
        return {}
    context = row["instagram_content_context"]
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except json.JSONDecodeError:
            return {}
    return context if isinstance(context, dict) else {}


def _context_datetime(value) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _row_to_session(row) -> ConversationSession:
    data = dict(row)
    data["customer_id"] = str(data.get("customer_id") or "")
    data["current_order_id"] = str(data["current_order_id"]) if data.get("current_order_id") else None
    try:
        return ConversationSession.model_validate(data)
    except ValidationError:
        return ConversationSession(customer_id=data["customer_id"])


def _customer_id_text(customer_id) -> str:
    return str(customer_id or "").strip()
