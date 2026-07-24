"""
Broadcast API endpoints.
Create, preview, list, and execute broadcasts.
"""

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.admin.auth import require_admin
from app.broadcast.sender import (
    create_broadcast,
    execute_broadcast,
    list_broadcast_deliveries,
    list_broadcasts,
    preview_broadcast,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/broadcasts", tags=["broadcasts"], dependencies=[Depends(require_admin)])


class BroadcastCreate(BaseModel):
    name: str
    template_name: str
    target_tags: list[str]
    template_params: list[str] | None = None
    target_channel: Literal["whatsapp"] = "whatsapp"
    scheduled_at: datetime | None = None


class BroadcastPreview(BaseModel):
    target_tags: list[str]
    target_channel: Literal["whatsapp"] = "whatsapp"


class DeliveryRetryConfirmation(BaseModel):
    confirmed_not_delivered: Literal[True]


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


@router.get("/{broadcast_id}/deliveries")
async def list_broadcast_deliveries_endpoint(broadcast_id: str, limit: int = 500):
    """Return per-recipient delivery outcomes for reconciliation."""
    return await list_broadcast_deliveries(broadcast_id, limit=limit)


@router.post("/{broadcast_id}/send")
async def send_broadcast_endpoint(broadcast_id: str):
    """Execute a draft or scheduled broadcast immediately."""
    from app.config import get_config

    if not getattr(get_config(), "outbound_processing_enabled", True):
        raise HTTPException(status_code=503, detail="Outbound processing is disabled.")
    result = await execute_broadcast(broadcast_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/{broadcast_id}/reset")
async def reset_broadcast_endpoint(broadcast_id: str):
    """Reset a stuck broadcast back to draft status."""
    from app import db
    from app.broadcast.sender import recover_stale_broadcast_deliveries

    broadcast = await db.fetch_one(
        "SELECT id, status FROM broadcasts WHERE id = :id",
        {"id": broadcast_id},
    )
    if not broadcast:
        raise HTTPException(status_code=404, detail="Broadcast not found")

    recovery = await recover_stale_broadcast_deliveries(broadcast_id)
    reset_known_failures = await db.execute(
        """
        UPDATE broadcast_deliveries
        SET status = 'pending', failed_at = NULL, outbound_started_at = NULL,
            last_error = NULL, updated_at = NOW()
        WHERE broadcast_id = :id
          AND status = 'failed'
          AND last_error = 'known_meta_send_failure'
        """,
        {"id": broadcast_id},
    )

    await db.execute(
        """
        UPDATE broadcasts
        SET status = 'draft',
            sent_at = NULL,
            recipients = (
                SELECT COUNT(*) FROM broadcast_deliveries
                WHERE broadcast_id = :id AND status = 'sent'
            )
        WHERE id = :id
        """,
        {"id": broadcast_id},
    )
    recovery["reset_known_failures"] = int(reset_known_failures or 0)
    return {"broadcast_id": broadcast_id, "status": "draft", "message": "Broadcast reset to draft", "recovery": recovery}


@router.post("/{broadcast_id}/deliveries/{delivery_id}/retry")
async def retry_reconciled_delivery_endpoint(
    broadcast_id: str, delivery_id: str, body: DeliveryRetryConfirmation
):
    """Retry an ambiguous delivery only after an operator confirms it was not delivered."""
    from app import db

    result = await db.fetch_one(
        """
        UPDATE broadcast_deliveries
        SET status = 'pending', failed_at = NULL, claimed_at = NULL,
            outbound_started_at = NULL, last_error = 'manual_reconciliation_retry', updated_at = NOW()
        WHERE id = :delivery_id AND broadcast_id = :broadcast_id
          AND status = 'delivery_unknown'
        RETURNING id
        """,
        {"delivery_id": delivery_id, "broadcast_id": broadcast_id},
    )
    if not result:
        raise HTTPException(status_code=404, detail="Ambiguous delivery not found")
    return {"delivery_id": delivery_id, "status": "pending"}
