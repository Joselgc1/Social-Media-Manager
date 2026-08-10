"""Signed, context-only Meta Instagram webhook used alongside Kommo."""

import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from app.config import channel_backend_for, get_config
from app.integrations.meta_context.parser import parse_instagram_context_events
from app.integrations.meta_context.service import (
    process_context_event,
    store_context_event,
)
from app.request_limits import limiter
from app.webhooks.meta_security import verify_meta_signature

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks/meta", tags=["meta-instagram-context"])


@router.get("/instagram-context")
@limiter.exempt
async def verify_instagram_context(request: Request):
    config = get_config()
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if (
        (config.meta_instagram_context_enabled or getattr(config, "meta_story_context_enabled", False))
        and mode == "subscribe"
        and token == config.instagram_verify_token
        and challenge is not None
    ):
        return Response(content=challenge, status_code=200)
    return Response(status_code=403)


@router.post("/instagram-context")
@limiter.exempt
async def handle_instagram_context(request: Request, background_tasks: BackgroundTasks):
    config = get_config()
    body = await request.body()
    if not (
        config.meta_instagram_context_enabled
        or getattr(config, "meta_story_context_enabled", False)
    ) or not verify_meta_signature(
        body,
        request.headers.get("X-Hub-Signature-256", ""),
        config.meta_app_secret,
    ):
        logger.warning("Rejected Meta Instagram context webhook signature")
        raise HTTPException(status_code=403, detail="Invalid signature")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    if channel_backend_for("instagram", config) == "meta":
        from app.webhooks.instagram import ingest_instagram_payload

        await ingest_instagram_payload(payload)
        return Response(content="EVENT_RECEIVED", status_code=200)

    for event in parse_instagram_context_events(payload):
        if event.event_type == "comment" and not config.meta_instagram_context_enabled:
            continue
        if event.event_type == "story_reply" and not getattr(config, "meta_story_context_enabled", False):
            continue
        if event.instagram_account_id != getattr(config, "instagram_account_id", ""):
            logger.warning("Ignored Meta Instagram context event for a different account")
            continue
        result = await store_context_event(event)
        if result.get("event_id") and result.get("status") == "created":
            background_tasks.add_task(
                process_context_event,
                result["event_id"],
            )
    return Response(content="EVENT_RECEIVED", status_code=200)
