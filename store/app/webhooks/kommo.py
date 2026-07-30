"""Kommo webhook endpoints for channel-provider mode."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from pydantic import ValidationError

from app.config import get_config
from app.crm.channel_mappings import lookup_by_lead_id
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
from app.integrations.kommo.state import sync_local_state_from_ai_mode
from app.integrations.kommo.webhook_parser import normalize_kommo_webhook, parse_nested_form
from app.integrations.meta_context.correlation import schedule_context_job_processing

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks/kommo", tags=["kommo"])


class SalesbotCallbackParseError(ValueError):
    """Raised when a Salesbot widget callback body cannot be parsed safely."""


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
            if event.interaction_type == "instagram_comment":
                logger.info(
                    "Kommo native Instagram comment ignored by private-message webhook path: %s",
                    _event_log_context(event),
                )
                continue
            if event.author_type and event.author_type != "external":
                logger.info("Kommo incoming message ignored because author is not external: %s", _event_log_context(event))
                continue
            if event.channel not in {"whatsapp", "instagram"}:
                logger.info("Kommo incoming message ignored for unsupported origin: %s", event.origin or "unknown")
                continue
            result = await record_incoming_event(event)
            logger.info("Kommo incoming message persisted result: %s", _safe_job_result(result))
            if (
                getattr(config, "outbound_processing_enabled", True)
                and result.get("status") in {"created", "merged"}
                and not launched_processing_task
            ):
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
        callback = await parse_salesbot_callback_request(request)
    except SalesbotCallbackParseError as e:
        logger.warning("Rejected malformed Kommo Salesbot callback: %s", sanitize_job_error(e))
        raise HTTPException(status_code=400, detail="Invalid Salesbot callback body") from e

    try:
        claims = validate_salesbot_jwt(callback.token, config)
        return_url = validate_return_url(callback.return_url, config.kommo_subdomain)
    except KommoAuthError as e:
        logger.warning("Rejected Kommo Salesbot callback: reason=%s", e.reason_code)
        raise HTTPException(status_code=401, detail="Invalid Salesbot callback") from e
    logger.info(
        "Kommo Salesbot callback validated: interaction_type=%s message_text_resolved=%s signed_entity_type=%s signed_entity_id=%s",
        callback.data.interaction_type or "private_message",
        _has_resolved_message_text(callback.data.message),
        claims.get("entity_type"),
        claims.get("entity_id"),
    )

    try:
        result = await persist_salesbot_callback(callback.data, return_url, claims)
    except ValueError as e:
        logger.warning("Rejected invalid Kommo Salesbot callback identity: %s", sanitize_job_error(e))
        raise HTTPException(status_code=400, detail="Invalid Salesbot callback body") from e

    if getattr(config, "outbound_processing_enabled", True) and result.get("status") == "ready":
        background_tasks.add_task(process_ready_jobs, 3)
    elif (
        getattr(config, "outbound_processing_enabled", True)
        and result.get("status") == "waiting_for_context"
    ):
        background_tasks.add_task(schedule_context_job_processing, result["job_id"])

    return {"status": "accepted"}


async def parse_salesbot_callback_request(request: Request) -> SalesbotWidgetRequest:
    content_type = request.headers.get("content-type", "")
    content_length = request.headers.get("content-length")
    payload = await _read_salesbot_callback_payload(request, content_type)
    callback = _coerce_salesbot_callback_payload(payload)
    logger.info(
        "Kommo Salesbot callback received: content_type=%s content_length=%s top_level_keys=%s data_field_names=%s",
        content_type.split(";", 1)[0] or "missing",
        content_length or "missing",
        sorted(map(str, payload.keys()))[:12],
        sorted(map(str, callback.data.model_dump(exclude_none=True).keys()))[:12],
    )
    return callback


async def _read_salesbot_callback_payload(request: Request, content_type: str) -> dict[str, Any]:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == "application/json":
        try:
            body = await request.json()
        except json.JSONDecodeError as e:
            raise SalesbotCallbackParseError("Invalid JSON callback body") from e
        return _normalize_callback_mapping(body)

    if media_type == "application/x-www-form-urlencoded":
        body = await request.body()
        return _parse_form_encoded_body(body)

    if media_type == "multipart/form-data":
        try:
            form = await request.form()
        except Exception as e:
            raise SalesbotCallbackParseError("Invalid multipart callback body") from e
        return _normalize_callback_mapping({key: value for key, value in form.multi_items() if isinstance(value, str)})

    body = await request.body()
    if not body:
        raise SalesbotCallbackParseError("Missing callback body")
    stripped = body.lstrip()
    if stripped.startswith((b"{", b"[")):
        try:
            return _normalize_callback_mapping(json.loads(body))
        except json.JSONDecodeError as e:
            raise SalesbotCallbackParseError("Invalid JSON callback body") from e
    return _parse_form_encoded_body(body)


def _parse_form_encoded_body(body: bytes) -> dict[str, Any]:
    if not body:
        raise SalesbotCallbackParseError("Missing callback body")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SalesbotCallbackParseError("Unreadable callback body") from e
    parsed = parse_qs(text, keep_blank_values=True)
    flat = {key: values[-1] if values else "" for key, values in parsed.items()}
    return _normalize_callback_mapping(flat)


def _normalize_callback_mapping(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SalesbotCallbackParseError("Callback body must be an object")
    if any("[" in str(key) for key in payload):
        payload = parse_nested_form(payload)
    return dict(payload)


def _coerce_salesbot_callback_payload(payload: dict[str, Any]) -> SalesbotWidgetRequest:
    values = dict(payload)
    data = values.get("data")
    if isinstance(data, str):
        if data.strip():
            try:
                values["data"] = json.loads(data)
            except json.JSONDecodeError as e:
                raise SalesbotCallbackParseError("Callback data field must be valid JSON") from e
        else:
            values["data"] = {}
    elif data is None:
        values["data"] = {}
    elif not isinstance(data, dict):
        raise SalesbotCallbackParseError("Callback data field must be an object")

    try:
        return SalesbotWidgetRequest.model_validate(values)
    except ValidationError as e:
        raise SalesbotCallbackParseError("Callback body is missing required fields") from e


async def _sync_lead_ai_mode(lead_id: str, enum_id: int) -> None:
    config = get_config()
    mapping = await lookup_by_lead_id("kommo", lead_id)
    if not mapping:
        return
    if enum_id in {config.kommo_ai_active_enum_id, config.kommo_ai_human_enum_id, config.kommo_ai_paused_enum_id}:
        await sync_local_state_from_ai_mode(str(mapping["customer_id"]), enum_id)


def _event_log_context(event) -> dict:
    return {
        "type": event.event_type,
        "channel": event.channel,
        "interaction_type": event.interaction_type,
        "origin": event.origin,
        "message_type": event.message_type,
        "post_id": event.post_id,
        "comment_id": event.comment_id,
        "parent_comment_id": event.parent_comment_id,
        "media_id": event.media_id,
        "has_post_url": bool(event.post_url),
        "has_comment_url": bool(event.comment_url),
        "author_id": event.author_id,
        "has_author_name": bool(event.author_name),
        "has_author_username": bool(event.author_username),
        "has_author_profile_url": bool(event.author_profile_url),
        "has_sender_username": bool(event.sender_username),
        "has_sender_profile_url": bool(event.sender_profile_url),
        "author_type": event.author_type,
        "has_text": bool(event.text),
        "has_media": bool(event.media_url),
        "message_id": event.message_id,
        "lead_id": event.lead_id,
        "contact_id": event.contact_id,
        "chat_id": event.chat_id,
        "talk_id": event.talk_id,
        "entity_id": event.entity_id,
        "entity_type": event.entity_type,
    }


def _has_resolved_message_text(value: str | None) -> bool:
    text = str(value or "").strip()
    return bool(text) and not (text.startswith("{{") and text.endswith("}}"))


def _safe_job_result(result: dict) -> dict:
    return {
        "status": result.get("status"),
        "job_id": result.get("job_id"),
        "reason": result.get("reason"),
    }
