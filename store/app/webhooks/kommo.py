"""Kommo webhook endpoints for channel-provider mode."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from app.config import get_config
from app.crm.channel_mappings import lookup_by_lead_id
from app.crm.customers import set_conversation_state
from app.integrations.kommo.auth import (
    KommoAuthError,
    validate_return_url,
    validate_salesbot_jwt,
    validate_webhook_secret,
)
from app.integrations.kommo.jobs import (
    persist_salesbot_callback,
    process_ready_jobs,
    record_incoming_event,
    sanitize_job_error,
    schedule_due_job_processing,
)
from app.integrations.kommo.models import SalesbotWidgetRequest
from app.integrations.kommo.webhook_parser import normalize_kommo_webhook

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks/kommo", tags=["kommo"])


@router.post("/events/{webhook_secret}")
async def handle_kommo_events(webhook_secret: str, request: Request):
    config = get_config()
    if not validate_webhook_secret(webhook_secret, config.kommo_webhook_secret):
        raise HTTPException(status_code=404, detail="Not found")

    form = await request.form()
    payload = {key: value for key, value in form.items() if isinstance(value, str)}
    events = normalize_kommo_webhook(payload)

    launched_processing_task = False
    for event in events:
        if event.event_type == "incoming_message":
            if event.author_type and event.author_type != "external":
                continue
            if event.channel not in {"whatsapp", "instagram"}:
                logger.info("Kommo incoming message ignored for unsupported origin: %s", event.origin or "unknown")
                continue
            result = await record_incoming_event(event)
            if result.get("status") in {"created", "merged"} and not launched_processing_task:
                asyncio.create_task(schedule_due_job_processing())
                launched_processing_task = True
            continue

        if event.event_type == "lead_updated" and event.ai_mode_enum_id is not None and event.lead_id:
            await _sync_lead_ai_mode(event.lead_id, event.ai_mode_enum_id)
            continue

        if event.event_type == "outgoing_message":
            logger.debug("Kommo outgoing message event recorded for diagnostics only.")

    return Response(content="OK", status_code=200)


@router.post("/salesbot")
async def handle_kommo_salesbot(request: Request, background_tasks: BackgroundTasks):
    config = get_config()
    try:
        body = await request.json()
        callback = SalesbotWidgetRequest.model_validate(body)
        claims = validate_salesbot_jwt(callback.token, config)
        return_url = validate_return_url(callback.return_url, config.kommo_subdomain)
    except (ValueError, KommoAuthError) as e:
        logger.warning("Rejected Kommo Salesbot callback: %s", sanitize_job_error(e))
        raise HTTPException(status_code=401, detail="Invalid Salesbot callback") from e

    result = await persist_salesbot_callback(callback.data, return_url, claims)
    if result.get("status") == "ready":
        background_tasks.add_task(process_ready_jobs, 3)

    return {"status": "accepted"}


async def _sync_lead_ai_mode(lead_id: str, enum_id: int) -> None:
    config = get_config()
    mapping = await lookup_by_lead_id("kommo", lead_id)
    if not mapping:
        return
    if enum_id == config.kommo_ai_active_enum_id:
        await set_conversation_state(str(mapping["customer_id"]), "active")
    elif enum_id in {config.kommo_ai_human_enum_id, config.kommo_ai_paused_enum_id}:
        await set_conversation_state(str(mapping["customer_id"]), "escalated")
