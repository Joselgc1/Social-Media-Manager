"""Pydantic models for normalized Kommo inputs and durable jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

KommoEntityType = Literal["leads", "contacts"]
KommoChannel = Literal["whatsapp", "instagram"]
KommoInteractionType = Literal["private_message", "instagram_comment"]
KommoEventType = Literal[
    "incoming_message",
    "outgoing_message",
    "lead_updated",
    "contact_updated",
    "talk_added",
    "talk_updated",
]
KommoJobStatus = Literal[
    "pending",
    "prepared",
    "waiting_for_salesbot",
    "waiting_for_context",
    "ready",
    "processing",
    "continuing",
    "sent",
    "discarded",
    "delivery_unknown",
    "failed",
]


class KommoIdentifiers(BaseModel):
    lead_id: str | None = None
    contact_id: str | None = None
    chat_id: str | None = None
    talk_id: str | None = None
    message_id: str | None = None
    entity_id: str | None = None
    entity_type: str | None = None
    origin: str | None = None
    interaction_type: KommoInteractionType = "private_message"


class NormalizedKommoEvent(BaseModel):
    event_type: KommoEventType
    message_id: str | None = None
    chat_id: str | None = None
    talk_id: str | None = None
    contact_id: str | None = None
    lead_id: str | None = None
    entity_id: str | None = None
    entity_type: str | None = None
    text: str | None = None
    message_type: str | None = None
    origin: str | None = None
    channel: KommoChannel | None = None
    author_id: str | None = None
    author_name: str | None = None
    author_type: str | None = None
    author_username: str | None = None
    author_profile_url: str | None = None
    sender_username: str | None = None
    sender_profile_url: str | None = None
    created_at: datetime | None = None
    ai_mode_enum_id: int | None = None
    media_url: str | None = None
    interaction_type: KommoInteractionType = "private_message"
    post_id: str | None = None
    comment_id: str | None = None
    parent_comment_id: str | None = None
    media_id: str | None = None
    post_url: str | None = None
    comment_url: str | None = None

    @property
    def stable_entity_id(self) -> str | None:
        return self.chat_id or self.contact_id or self.talk_id or self.entity_id or self.lead_id

    @property
    def correlation_id(self) -> str:
        stable = self.chat_id or self.contact_id or self.talk_id or self.entity_id or self.lead_id
        return f"kommo:{self.interaction_type}:{stable or self.message_id or 'unknown'}"


class IncomingMessageEvent(NormalizedKommoEvent):
    event_type: Literal["incoming_message"] = "incoming_message"


class OutgoingMessageEvent(NormalizedKommoEvent):
    event_type: Literal["outgoing_message"] = "outgoing_message"


class LeadUpdateEvent(NormalizedKommoEvent):
    event_type: Literal["lead_updated"] = "lead_updated"


class TalkEvent(NormalizedKommoEvent):
    event_type: Literal["talk_added", "talk_updated"]


class SalesbotWidgetData(BaseModel):
    message: str | None = None
    lead_id: str | None = None
    contact_id: str | None = None
    origin: str | None = None
    responsible_user_id: str | None = None
    chat_id: str | None = None
    talk_id: str | None = None
    author_username: str | None = None
    author_profile_url: str | None = None
    sender_username: str | None = None
    sender_profile_url: str | None = None
    interaction_type: KommoInteractionType | None = None
    expected_channel: KommoChannel | None = None
    post_id: str | None = None
    comment_id: str | None = None
    parent_comment_id: str | None = None
    media_id: str | None = None
    post_url: str | None = None
    comment_url: str | None = None
    post_caption: str | None = None
    post_text: str | None = None
    media_caption: str | None = None
    product_name: str | None = None
    post_product_name: str | None = None
    product_sku: str | None = None
    parent_sku: str | None = None
    image_url: str | None = None
    post_image_url: str | None = None
    post_media_url: str | None = None

    @field_validator(
        "lead_id",
        "contact_id",
        "responsible_user_id",
        "chat_id",
        "talk_id",
        "author_username",
        "author_profile_url",
        "sender_username",
        "sender_profile_url",
        "post_id",
        "comment_id",
        "parent_comment_id",
        "media_id",
        "post_url",
        "comment_url",
        "post_caption",
        "post_text",
        "media_caption",
        "product_name",
        "post_product_name",
        "product_sku",
        "parent_sku",
        "image_url",
        "post_image_url",
        "post_media_url",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value):
        if value in ("", None):
            return None
        text = str(value).strip()
        if text.startswith("{{") and text.endswith("}}"):
            return None
        return text

    @field_validator("interaction_type", mode="before")
    @classmethod
    def _normalize_interaction_type(cls, value):
        if value in ("", None):
            return None
        text = str(value).strip().lower()
        if text.startswith("{{") and text.endswith("}}"):
            return None
        if text in {"private_message", "instagram_comment"}:
            return text

    @field_validator("expected_channel", mode="before")
    @classmethod
    def _normalize_expected_channel(cls, value):
        if value in ("", None):
            return None
        text = str(value).strip().lower()
        if text.startswith("{{") and text.endswith("}}"):
            return None
        return text


class SalesbotWidgetRequest(BaseModel):
    token: str
    data: SalesbotWidgetData = Field(default_factory=SalesbotWidgetData)
    return_url: str


class PersistentKommoJob(BaseModel):
    id: str
    correlation_id: str
    external_message_id: str | None = None
    lead_id: str | None = None
    contact_id: str | None = None
    chat_id: str | None = None
    talk_id: str | None = None
    origin: str | None = None
    channel: str | None = None
    interaction_type: KommoInteractionType = "private_message"
    author_id: str | None = None
    author_name: str | None = None
    author_username: str | None = None
    author_profile_url: str | None = None
    sender_username: str | None = None
    sender_profile_url: str | None = None
    combined_message: str
    media_url: str | None = None
    return_url: str | None = None
    public_comment_context: dict | None = None
    meta_context_event_id: str | None = None
    context_status: Literal[
        "not_required", "pending", "matched", "ambiguous", "timed_out"
    ] = "not_required"
    context_deadline_at: datetime | None = None
    context_correlation_score: int | None = None
    status: KommoJobStatus
    attempt_count: int = 0
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    processing_started_at: datetime | None = None
    processing_lease_id: str | None = None
    ai_started_at: datetime | None = None
    completed_at: datetime | None = None


class NormalizedResponseOutput(BaseModel):
    customer_text: str | None = None
    discarded: bool = False
    reason: str | None = None
