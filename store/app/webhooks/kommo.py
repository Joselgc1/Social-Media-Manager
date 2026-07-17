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

    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        body = await request.json()
        payload = body if isinstance(body, dict) else {}
    else:
        form = await request.form()
        payload = {key: value for key, value in form.items() if isinstance(value, str)}
    events = normalize_kommo_webhook(payload)
    logger.info(
        "Kommo webhook normalized %s event(s) from top-level keys: %s",
        len(events),
        sorted(map(str, payload.keys()))[:8],
    )

    launched_processing_task = False
    for event in events:
        logger.info("Kommo event received: %s", _event_log_context(event))
        if event.event_type == "incoming_message":
            if event.author_type and event.author_type != "external":
                logger.info("Kommo incoming message ignored because author is not external: %s", _event_log_context(event))
                continue
            if event.channel not in {"whatsapp", "instagram"}:
                logger.info("Kommo incoming message ignored for unsupported origin: %s", event.origin or "unknown")
                continue
            result = await record_incoming_event(event)
            logger.info("Kommo incoming message persisted result: %s", _safe_job_result(result))
            if result.get("status") in {"created", "merged"} and not launched_processing_task:
                asyncio.create_task(schedule_due_job_processing())
                launched_processing_task = True
            continue

        if event.event_type == "lead_updated" and event.ai_mode_enum_id is not None and event.lead_id:
            await _sync_lead_ai_mode(event.lead_id, event.ai_mode_enum_id)
            continue

        if event.event_type == "outgoing_message":
            logger.info("Kommo outgoing message event ignored for auto-reply: %s", _event_log_context(event))

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


def _event_log_context(event) -> dict:
    return {
        "type": event.event_type,
        "channel": event.channel,
        "origin": event.origin,
        "author_type": event.author_type,
        "has_text": bool(event.text),
        "has_media": bool(event.media_url),
        "message_id": event.message_id,
        "lead_id": event.lead_id,
        "contact_id": event.contact_id,
        "chat_id": event.chat_id,
        "talk_id": event.talk_id,
    }


def _safe_job_result(result: dict) -> dict:
    return {
        "status": result.get("status"),
        "job_id": result.get("job_id"),
        "discarded_job_id": result.get("discarded_job_id"),
        "reason": result.get("reason"),
    }
