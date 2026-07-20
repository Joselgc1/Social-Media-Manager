"""Pydantic models for normalized Kommo inputs and durable jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

KommoEntityType = Literal["leads", "contacts"]
KommoChannel = Literal["whatsapp", "instagram"]
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
    author_type: str | None = None
    created_at: datetime | None = None
    ai_mode_enum_id: int | None = None
    media_url: str | None = None

    @property
    def stable_entity_id(self) -> str | None:
        return self.lead_id or self.entity_id or self.contact_id or self.chat_id or self.talk_id

    @property
    def correlation_id(self) -> str:
        stable = self.lead_id or self.entity_id or self.chat_id or self.contact_id or self.talk_id
        return f"kommo:{stable or self.message_id or 'unknown'}"


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

    @field_validator("lead_id", "contact_id", "responsible_user_id", "chat_id", "talk_id", mode="before")
    @classmethod
    def _blank_to_none(cls, value):
        if value in ("", None):
            return None
        text = str(value).strip()
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
    combined_message: str
    media_url: str | None = None
    return_url: str | None = None
    status: KommoJobStatus
    attempt_count: int = 0
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    processing_started_at: datetime | None = None
    completed_at: datetime | None = None


class NormalizedResponseOutput(BaseModel):
    customer_text: str | None = None
    discarded: bool = False
    reason: str | None = None
