"""Normalized models for authoritative Meta Instagram events."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


class MetaInstagramContextEvent(BaseModel):
    external_event_id: str
    event_type: Literal["comment", "story_reply"] = "comment"
    instagram_account_id: str | None = None
    sender_id: str | None = None
    sender_username: str | None = None
    message_text: str = ""
    event_timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    comment_id: str | None = None
    parent_comment_id: str | None = None
    message_id: str | None = None
    media_id: str | None = None
    media_product_type: str | None = None
    story_id: str | None = None
    story_url: str | None = None


class MetaMediaDetails(BaseModel):
    id: str
    permalink: str | None = None
    caption: str | None = None
    media_type: str | None = None
    media_product_type: str | None = None
    timestamp: datetime | None = None
    thumbnail_url: str | None = None
