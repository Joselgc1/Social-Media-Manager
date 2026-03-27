"""
Broadcast API endpoints.
Create, preview, list, and execute broadcasts.
"""

import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.broadcast.sender import (
    create_broadcast,
    execute_broadcast,
    list_broadcasts,
    preview_broadcast,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/broadcasts", tags=["broadcasts"])


class BroadcastCreate(BaseModel):
    name: str
    template_name: str
    target_tags: list[str]
    template_params: list[str] | None = None
    target_channel: str = "whatsapp"
    scheduled_at: datetime | None = None


class BroadcastPreview(BaseModel):
    target_tags: list[str]
    target_channel: str = "whatsapp"


@router.post("/create")
async def create_broadcast_endpoint(body: BroadcastCreate):
    """Create a new broadcast (defaults to draft status)."""
    result = await create_broadcast(
        name=body.name,
        template_name=body.template_name,
        target_tags=body.target_tags,
        template_params=body.template_params,
        target_channel=body.target_channel,
        scheduled_at=body.scheduled_at,
    )
    return result


@router.post("/preview")
async def preview_broadcast_endpoint(body: BroadcastPreview):
    """Preview how many customers match the target tags."""
    result = await preview_broadcast(
        target_tags=body.target_tags,
        channel=body.target_channel,
    )
    return result


@router.get("/list")
async def list_broadcasts_endpoint(limit: int = 20):
    """List recent broadcasts."""
    return await list_broadcasts(limit=limit)


@router.post("/{broadcast_id}/send")
async def send_broadcast_endpoint(broadcast_id: str):
    """Execute a draft or scheduled broadcast immediately."""
    result = await execute_broadcast(broadcast_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result
