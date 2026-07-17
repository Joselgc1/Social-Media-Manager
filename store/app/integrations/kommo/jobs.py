"""Durable PostgreSQL-backed Kommo job processing."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from app import db
from app.ai.engine import generate_response
from app.config import get_config
from app.crm import conversations
from app.crm.channel_mappings import resolve_customer_from_kommo_job, upsert_mapping
from app.integrations.kommo.client import KommoAPIError, KommoClient, sanitize_kommo_error
from app.integrations.kommo.models import NormalizedKommoEvent, SalesbotWidgetData
from app.integrations.kommo.response_mapper import map_ai_response_to_salesbot
from app.integrations.kommo.state import (
    ensure_ai_mode_initialized,
    evaluate_automation_state,
    extract_ai_mode_enum_from_lead,
    sync_local_state_from_ai_mode,
)
from app.webhooks.inbound_buffer import MESSAGE_DEBOUNCE_SECONDS

logger = logging.getLogger(__name__)

MAX_JOB_ATTEMPTS = 3
STALE_PROCESSING_MINUTES = 5
STALE_WAITING_MINUTES = 30
_TERMINAL_STATUSES = {"sent", "discarded", "failed", "delivery_unknown"}
_ACTIVE_SALESBOT_STATUSES = {"prepared", "waiting_for_salesbot", "ready", "processing", "continuing"}
_TRANSIENT_CONTINUATION_STATUSES = {429, 500, 502, 503, 504}
_FINISH_HANDLERS = [{"handler": "goto", "params": {"type": "finish", "step": 0}}]


def sanitize_job_error(error: Exception | str) -> str:
    return sanitize_kommo_error(error)


async def record_incoming_event(event: NormalizedKommoEvent) -> dict:
    """Persist an inbound message and merge it into any still-pending debounce job."""
    external_message_id = event.message_id or f"{event.correlation_id}:{event.created_at or datetime.now(UTC)}"
    text = (event.text or "").strip()
    if not text:
        text = _message_placeholder(event)

    async with db.get_db().transaction():
        await db.fetch_one(
            "SELECT pg_advisory_xact_lock(hashtext(:correlation_id))",
            {"correlation_id": event.correlation_id},
        )
        existing = await db.fetch_one(
            "SELECT id, status FROM kommo_message_jobs WHERE external_message_id = :external_message_id",
            {"external_message_id": external_message_id},
        )
        if existing:
            return {"status": "duplicate", "job_id": str(existing["id"])}

        pending = await db.fetch_one(
            """
            SELECT id, combined_message
            FROM kommo_message_jobs
            WHERE correlation_id = :correlation_id
              AND status = 'pending'
            ORDER BY created_at DESC
            FOR UPDATE
            LIMIT 1
            """,
            {"correlation_id": event.correlation_id},
        )
        if pending:
            merged = "\n".join(part for part in (pending["combined_message"], text) if part)
            await db.execute(
                """
                UPDATE kommo_message_jobs
                SET combined_message = :combined_message,
                    media_url = COALESCE(:media_url, media_url),
                    updated_at = NOW(),
                    buffer_expires_at = NOW() + (:debounce_seconds * INTERVAL '1 second')
                WHERE id = :id
                """,
                {
                    "combined_message": merged,
                    "media_url": event.media_url,
                    "debounce_seconds": MESSAGE_DEBOUNCE_SECONDS,
                    "id": pending["id"],
                },
            )
            merged_job_id = str(pending["id"])
            inserted_id = await db.execute(
                """
                INSERT INTO kommo_message_jobs (
                    correlation_id, external_message_id, lead_id, contact_id, chat_id, talk_id,
                    origin, channel, combined_message, media_url, status, last_error, buffer_expires_at
                ) VALUES (
                    :correlation_id, :external_message_id, :lead_id, :contact_id, :chat_id, :talk_id,
                    :origin, :channel, :combined_message, :media_url, 'discarded', :last_error,
                    NOW() + (:debounce_seconds * INTERVAL '1 second')
                )
                RETURNING id
                """,
                _event_values(event, external_message_id, text)
                | {
                    "last_error": f"Merged into pending job {merged_job_id}",
                    "debounce_seconds": MESSAGE_DEBOUNCE_SECONDS,
                },
            )
            return {"status": "merged", "job_id": merged_job_id, "discarded_job_id": str(inserted_id)}

        job_id = await db.execute(
            """
            INSERT INTO kommo_message_jobs (
                correlation_id, external_message_id, lead_id, contact_id, chat_id, talk_id,
                origin, channel, combined_message, media_url, status, buffer_expires_at
            ) VALUES (
                :correlation_id, :external_message_id, :lead_id, :contact_id, :chat_id, :talk_id,
                :origin, :channel, :combined_message, :media_url, 'pending',
                NOW() + (:debounce_seconds * INTERVAL '1 second')
            )
            RETURNING id
            """,
            _event_values(event, external_message_id, text)
            | {"debounce_seconds": MESSAGE_DEBOUNCE_SECONDS},
        )
        return {"status": "created", "job_id": str(job_id)}


async def schedule_due_job_processing(delay_seconds: float | None = None) -> None:
    await asyncio.sleep(delay_seconds if delay_seconds is not None else MESSAGE_DEBOUNCE_SECONDS + 0.2)
    await process_pending_jobs(limit=5)


async def process_pending_jobs(limit: int = 10) -> int:
    if get_config().channel_backend != "kommo":
        return 0
    processed = 0
    for _ in range(limit):
        job = await _claim_due_pending_job()
        if not job:
            break
        await _launch_salesbot_for_job(dict(job))
        processed += 1
    return processed


async def process_ready_jobs(limit: int = 5) -> int:
    if get_config().channel_backend != "kommo":
        return 0
    processed = 0
    for _ in range(limit):
        job = await _claim_ready_job()
        if not job:
            break
        await _process_ready_job(dict(job))
        processed += 1
    return processed


async def persist_salesbot_callback(data: SalesbotWidgetData, return_url: str, claims: dict | None = None) -> dict:
    values = _callback_values(data, return_url, claims or {})
    if not (values.get("lead_id") or values.get("contact_id")):
        return {"status": "ignored", "reason": "missing_entity_id"}

    job = await db.fetch_one(
        """
        UPDATE kommo_message_jobs job
        SET return_url = :return_url,
            status = 'ready',
            callback_claims = CAST(:callback_claims AS jsonb),
            salesbot_token_jti = :salesbot_token_jti,
            salesbot_account_id = :salesbot_account_id,
            salesbot_user_id = :salesbot_user_id,
            salesbot_client_uuid = :salesbot_client_uuid,
            updated_at = NOW()
        WHERE job.id = (
            SELECT candidate.id
            FROM kommo_message_jobs candidate
            WHERE candidate.status = 'waiting_for_salesbot'
              AND (
                  (:lead_id IS NOT NULL AND candidate.lead_id = :lead_id)
                  OR (:lead_id IS NULL AND :contact_id IS NOT NULL AND candidate.contact_id = :contact_id)
              )
            ORDER BY candidate.salesbot_launched_at DESC NULLS LAST, candidate.created_at DESC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING *
        """,
        values,
    )
    if job:
        return {"status": "ready", "job_id": str(job["id"])}

    latest = await _find_latest_job_for_callback(data)
    if latest and latest["status"] in _TERMINAL_STATUSES | _ACTIVE_SALESBOT_STATUSES:
        return {"status": "duplicate", "job_id": str(latest["id"])}
    return {"status": "ignored", "reason": "no_waiting_job"}


async def recover_stale_jobs() -> dict:
    failed_waiting = await db.execute(
        """
        UPDATE kommo_message_jobs
        SET status = 'failed',
            last_error = 'Salesbot callback did not arrive before stale timeout',
            updated_at = NOW(),
            completed_at = NOW()
        WHERE status = 'waiting_for_salesbot'
          AND salesbot_launched_at < NOW() - (:minutes * INTERVAL '1 minute')
        """,
        {"minutes": STALE_WAITING_MINUTES},
    )
    reset_processing = await db.execute(
        """
        UPDATE kommo_message_jobs
        SET status = CASE WHEN return_url IS NULL THEN 'pending' ELSE 'ready' END,
            processing_started_at = NULL,
            updated_at = NOW(),
            last_error = 'Recovered stale processing job'
        WHERE status = 'processing'
          AND processing_started_at < NOW() - (:minutes * INTERVAL '1 minute')
          AND attempt_count < :max_attempts
        """,
        {"minutes": STALE_PROCESSING_MINUTES, "max_attempts": MAX_JOB_ATTEMPTS},
    )
    marked_delivery_unknown = await db.execute(
        """
        UPDATE kommo_message_jobs
        SET status = 'delivery_unknown',
            completed_at = NOW(),
            updated_at = NOW(),
            processing_started_at = NULL,
            last_error = 'Continuation outcome unknown after stale timeout'
        WHERE status = 'continuing'
          AND processing_started_at < NOW() - (:minutes * INTERVAL '1 minute')
        """,
        {"minutes": STALE_PROCESSING_MINUTES},
    )
    marked_failed = await db.execute(
        """
        UPDATE kommo_message_jobs
        SET status = 'failed',
            completed_at = NOW(),
            updated_at = NOW(),
            last_error = 'Max attempts exceeded during stale recovery'
        WHERE status = 'processing'
          AND processing_started_at < NOW() - (:minutes * INTERVAL '1 minute')
          AND attempt_count >= :max_attempts
        """,
        {"minutes": STALE_PROCESSING_MINUTES, "max_attempts": MAX_JOB_ATTEMPTS},
    )
    return {
        "failed_waiting": failed_waiting,
        "reset_processing": reset_processing,
        "marked_delivery_unknown": marked_delivery_unknown,
        "marked_failed": marked_failed,
    }


async def diagnostics_summary() -> dict:
    rows = await db.fetch_all(
        """
        SELECT status, COUNT(*) AS cnt
        FROM kommo_message_jobs
        GROUP BY status
        """
    )
    counts = {row["status"]: row["cnt"] for row in rows}
    timestamps = await db.fetch_one(
        """
        SELECT
            MAX(created_at) FILTER (WHERE external_message_id IS NOT NULL) AS last_incoming,
            MAX(salesbot_launched_at) AS last_launch,
            MAX(updated_at) FILTER (WHERE return_url IS NOT NULL) AS last_callback,
            MAX(completed_at) FILTER (WHERE status = 'sent') AS last_continuation,
            MAX(last_error) FILTER (WHERE last_error IS NOT NULL) AS last_error
        FROM kommo_message_jobs
        """
    )
    stale = await db.fetch_one(
        """
        SELECT COUNT(*) AS cnt
        FROM kommo_message_jobs
        WHERE status IN ('processing', 'continuing')
          AND processing_started_at < NOW() - (:minutes * INTERVAL '1 minute')
        """,
        {"minutes": STALE_PROCESSING_MINUTES},
    )
    return {
        "pending_job_count": counts.get("pending", 0) + counts.get("prepared", 0) + counts.get("waiting_for_salesbot", 0) + counts.get("ready", 0) + counts.get("processing", 0) + counts.get("continuing", 0),
        "failed_job_count": counts.get("failed", 0) + counts.get("delivery_unknown", 0),
        "stale_job_count": stale["cnt"] if stale else 0,
        "last_successful_incoming_webhook_at": timestamps["last_incoming"] if timestamps else None,
        "last_successful_salesbot_launch_at": timestamps["last_launch"] if timestamps else None,
        "last_successful_widget_callback_at": timestamps["last_callback"] if timestamps else None,
        "last_successful_continuation_at": timestamps["last_continuation"] if timestamps else None,
        "last_kommo_api_error_summary": sanitize_job_error(timestamps["last_error"]) if timestamps and timestamps["last_error"] else None,
    }


async def _claim_due_pending_job():
    return await db.fetch_one(
        """
        UPDATE kommo_message_jobs job
        SET status = 'processing',
            processing_started_at = NOW(),
            attempt_count = attempt_count + 1,
            updated_at = NOW()
        WHERE job.id = (
            SELECT candidate.id
            FROM kommo_message_jobs candidate
            WHERE candidate.status = 'pending'
              AND candidate.buffer_expires_at <= NOW()
              AND NOT EXISTS (
                  SELECT 1 FROM kommo_message_jobs active
                  WHERE active.correlation_id = candidate.correlation_id
                    AND active.status IN ('prepared', 'waiting_for_salesbot', 'ready', 'processing', 'continuing')
                    AND active.id <> candidate.id
              )
            ORDER BY candidate.created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING *
        """
    )


async def _claim_ready_job():
    return await db.fetch_one(
        """
        UPDATE kommo_message_jobs job
        SET status = 'processing',
            processing_started_at = NOW(),
            attempt_count = attempt_count + 1,
            updated_at = NOW()
        WHERE job.id = (
            SELECT candidate.id
            FROM kommo_message_jobs candidate
            WHERE candidate.status = 'ready'
              AND candidate.return_url IS NOT NULL
            ORDER BY candidate.updated_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING *
        """
    )


async def _launch_salesbot_for_job(job: dict) -> None:
    try:
        settings = await db.get_settings()
        client = KommoClient.from_config()
        lead = await client.get_lead(job["lead_id"]) if job.get("lead_id") else None
        ai_mode_enum = None
        if lead:
            ai_mode_enum, initialized_now = await ensure_ai_mode_initialized(client, str(job["lead_id"]), lead)
            if initialized_now:
                logger.info("Initialized Kommo AI Mode for lead %s", job["lead_id"])

        customer = await resolve_customer_from_kommo_job(job, lead=lead)
        if ai_mode_enum is not None:
            await sync_local_state_from_ai_mode(customer["id"], ai_mode_enum)
            customer["conversation_state"] = "active" if ai_mode_enum == get_config().kommo_ai_active_enum_id else "escalated"

        decision = evaluate_automation_state(
            ai_enabled=bool(settings.get("ai_enabled", True)),
            local_conversation_state=customer.get("conversation_state"),
            kommo_ai_mode_enum_id=ai_mode_enum,
            job_status=job.get("status"),
        )
        if not decision.allowed:
            await _store_user_message_if_suppressed(customer, job)
            await _mark_job(job["id"], "discarded" if not decision.needs_ai_mode_initialization else "failed", decision.reason)
            return

        entity_id = job.get("lead_id") or job.get("contact_id")
        entity_type = "leads" if job.get("lead_id") else "contacts"
        if not entity_id:
            await _mark_job(job["id"], "failed", "missing_entity_id")
            return
        await client.run_salesbot(entity_id, entity_type)
        await db.execute(
            """
            UPDATE kommo_message_jobs
            SET status = 'waiting_for_salesbot',
                salesbot_launched_at = NOW(),
                processing_started_at = NULL,
                updated_at = NOW()
            WHERE id = :id AND status = 'processing'
            """,
            {"id": job["id"]},
        )
    except Exception as e:
        await _mark_job(job["id"], "failed", sanitize_job_error(e))


async def _process_ready_job(job: dict) -> None:
    config = get_config()
    client = KommoClient.from_config()
    continuation_started = False
    try:
        settings = await db.get_settings()
        lead = await client.get_lead(job["lead_id"]) if job.get("lead_id") else None
        ai_mode_enum = extract_ai_mode_enum_from_lead(lead or {}, config) if lead else config.kommo_ai_active_enum_id
        customer = await resolve_customer_from_kommo_job(job, lead=lead)
        if ai_mode_enum is not None:
            await sync_local_state_from_ai_mode(customer["id"], ai_mode_enum)
            customer["conversation_state"] = "active" if ai_mode_enum == config.kommo_ai_active_enum_id else "escalated"

        before = evaluate_automation_state(
            ai_enabled=bool(settings.get("ai_enabled", True)),
            local_conversation_state=customer.get("conversation_state"),
            kommo_ai_mode_enum_id=ai_mode_enum,
            job_status=None,
            config=config,
        )
        if not before.allowed:
            await _store_user_message_if_suppressed(customer, job)
            await _continue_and_discard_job(client, job, before.reason)
            return

        sender_id = _local_sender_id(job)
        media_url = job.get("media_url")
        result = await generate_response(
            channel=job.get("channel") or "whatsapp",
            sender_id=sender_id,
            message_text=job["combined_message"],
            media_url=media_url,
            customer_profile=_customer_profile_from_lead(job, lead),
            customer_id=str(customer["id"]),
            integration_context={
                "provider": "kommo",
                "lead_id": job.get("lead_id"),
                "contact_id": job.get("contact_id"),
                "chat_id": job.get("chat_id"),
                "talk_id": job.get("talk_id"),
                "media_url_is_direct": bool(job.get("media_url")),
            },
            persist_assistant_message=False,
        )
        if result.get("customer_id"):
            await upsert_mapping(
                customer_id=result["customer_id"],
                provider="kommo",
                channel=job.get("channel") or "whatsapp",
                external_contact_id=job.get("contact_id"),
                external_lead_id=job.get("lead_id"),
                external_chat_id=job.get("chat_id"),
                external_talk_id=job.get("talk_id"),
                external_origin=job.get("origin"),
            )

        if not result.get("escalated"):
            lead_after = await client.get_lead(job["lead_id"]) if job.get("lead_id") else None
            mode_after = extract_ai_mode_enum_from_lead(lead_after or {}, config) if lead_after else ai_mode_enum
            refreshed_customer = await db.fetch_one("SELECT conversation_state FROM customers WHERE id = :id", {"id": result.get("customer_id") or customer["id"]})
            after = evaluate_automation_state(
                ai_enabled=bool((await db.get_settings()).get("ai_enabled", True)),
                local_conversation_state=refreshed_customer["conversation_state"] if refreshed_customer else customer.get("conversation_state"),
                kommo_ai_mode_enum_id=mode_after,
                job_status=None,
                config=config,
            )
            if not after.allowed:
                await _continue_and_discard_job(client, job, after.reason)
                return

        mapped = map_ai_response_to_salesbot(result)
        if mapped.discarded:
            await _continue_and_discard_job(client, job, mapped.reason or "empty_response")
            return
        await _mark_job_continuing(job["id"], mapped.execute_handlers)
        continuation_started = True
        response_payload = await client.continue_salesbot(job["return_url"], mapped.execute_handlers)
        await _mark_job_sent(job["id"], response_payload)
        await _store_assistant_message_after_delivery(customer, job, result, mapped.customer_text)
    except KommoAPIError as e:
        if not continuation_started and job.get("return_url"):
            await _continue_and_discard_job(client, job, sanitize_job_error(e))
            return
        await _mark_job(job["id"], _status_after_continuation_error(e, continuation_started), sanitize_job_error(e))
    except Exception as e:
        if not continuation_started and job.get("return_url"):
            await _continue_and_discard_job(client, job, sanitize_job_error(e))
            return
        await _mark_job(job["id"], "delivery_unknown" if continuation_started else "failed", sanitize_job_error(e))


async def _find_latest_job_for_callback(data: SalesbotWidgetData):
    clauses = []
    values = {}
    if data.lead_id:
        clauses.append("lead_id = :lead_id")
        values["lead_id"] = data.lead_id
    elif data.contact_id:
        clauses.append("contact_id = :contact_id")
        values["contact_id"] = data.contact_id
    else:
        return None
    query = f"""
        SELECT * FROM kommo_message_jobs
        WHERE {' AND '.join(clauses)}
        ORDER BY salesbot_launched_at DESC NULLS LAST, created_at DESC
        LIMIT 1
    """
    return await db.fetch_one(query, values)


def _callback_values(data: SalesbotWidgetData, return_url: str, claims: dict) -> dict:
    return {
        "return_url": return_url,
        "lead_id": data.lead_id,
        "contact_id": data.contact_id,
        "callback_claims": json.dumps(_safe_claims(claims)),
        "salesbot_token_jti": _claim_as_str(claims, "jti"),
        "salesbot_account_id": _claim_as_str(claims, "account_id"),
        "salesbot_user_id": _claim_as_str(claims, "user_id"),
        "salesbot_client_uuid": _claim_as_str(claims, "client_uuid") or _claim_as_str(claims, "client_uid"),
    }


def _safe_claims(claims: dict) -> dict:
    allowed = {"iss", "aud", "jti", "iat", "nbf", "exp", "account_id", "user_id", "subdomain", "client_uuid", "client_uid"}
    return {key: claims[key] for key in allowed if key in claims}


def _claim_as_str(claims: dict, key: str) -> str | None:
    value = claims.get(key)
    return str(value) if value not in (None, "") else None


async def _continue_and_discard_job(client: KommoClient, job: dict, reason: str | None) -> None:
    continuation_started = False
    try:
        await _mark_job_continuing(job["id"], _FINISH_HANDLERS)
        continuation_started = True
        response_payload = await client.continue_salesbot(job["return_url"], _FINISH_HANDLERS)
        await _mark_job_discarded(job["id"], reason, response_payload)
    except KommoAPIError as e:
        await _mark_job(job["id"], _status_after_continuation_error(e, continuation_started), sanitize_job_error(e))
    except Exception as e:
        await _mark_job(job["id"], "delivery_unknown" if continuation_started else "failed", sanitize_job_error(e))


async def _mark_job_continuing(job_id: str, execute_handlers: list[dict]) -> None:
    await db.execute(
        """
        UPDATE kommo_message_jobs
        SET status = 'continuing',
            continuation_payload = CAST(:continuation_payload AS jsonb),
            processing_started_at = NOW(),
            updated_at = NOW()
        WHERE id = :id AND status = 'processing'
        """,
        {"id": job_id, "continuation_payload": json.dumps({"execute_handlers": execute_handlers})},
    )


async def _mark_job_sent(job_id: str, response_payload) -> None:
    await db.execute(
        """
        UPDATE kommo_message_jobs
        SET status = 'sent',
            last_error = NULL,
            continuation_response = CAST(:continuation_response AS jsonb),
            processing_started_at = NULL,
            completed_at = NOW(),
            updated_at = NOW()
        WHERE id = :id
        """,
        {"id": job_id, "continuation_response": json.dumps({"response": response_payload})},
    )


async def _mark_job_discarded(job_id: str, reason: str | None, response_payload) -> None:
    await db.execute(
        """
        UPDATE kommo_message_jobs
        SET status = 'discarded',
            last_error = :last_error,
            continuation_response = CAST(:continuation_response AS jsonb),
            processing_started_at = NULL,
            completed_at = NOW(),
            updated_at = NOW()
        WHERE id = :id
        """,
        {
            "id": job_id,
            "last_error": sanitize_job_error(reason) if reason else None,
            "continuation_response": json.dumps({"response": response_payload}),
        },
    )


async def _store_assistant_message_after_delivery(customer: dict, job: dict, result: dict, customer_text: str | None) -> None:
    content = customer_text or result.get("text")
    if not content:
        return
    try:
        await conversations.store_message(
            customer_id=customer["id"],
            role="assistant",
            content=content,
            channel=job.get("channel") or customer.get("channel") or "whatsapp",
            function_calls=result.get("function_calls"),
        )
    except Exception as e:
        logger.warning("Failed to persist delivered Kommo assistant message: %s", sanitize_job_error(e))


def _status_after_continuation_error(error: KommoAPIError, continuation_started: bool) -> str:
    if not continuation_started:
        return "failed"
    if error.status_code is None or error.status_code in _TRANSIENT_CONTINUATION_STATUSES:
        return "delivery_unknown"
    return "failed"


async def _mark_job(job_id: str, status: str, error: str | None) -> None:
    await db.execute(
        """
        UPDATE kommo_message_jobs
        SET status = :status,
            last_error = :last_error,
            processing_started_at = NULL,
            completed_at = CASE WHEN :status IN ('sent', 'discarded', 'failed', 'delivery_unknown') THEN NOW() ELSE completed_at END,
            updated_at = NOW()
        WHERE id = :id
        """,
        {"status": status, "last_error": sanitize_job_error(error) if error else None, "id": job_id},
    )


async def _store_user_message_if_suppressed(customer: dict, job: dict) -> None:
    await conversations.store_message(
        customer_id=customer["id"],
        role="user",
        content=job["combined_message"],
        channel=job.get("channel") or customer.get("channel") or "whatsapp",
        media_url=job.get("media_url"),
    )


def _event_values(event: NormalizedKommoEvent, external_message_id: str, text: str) -> dict:
    return {
        "correlation_id": event.correlation_id,
        "external_message_id": external_message_id,
        "lead_id": event.lead_id,
        "contact_id": event.contact_id,
        "chat_id": event.chat_id,
        "talk_id": event.talk_id,
        "origin": event.origin,
        "channel": event.channel,
        "combined_message": text,
        "media_url": event.media_url,
    }


def _message_placeholder(event: NormalizedKommoEvent) -> str:
    if event.media_url:
        return "El cliente envio una imagen por Kommo."
    if event.message_type:
        return f"[Mensaje de tipo no soportado por Kommo: {event.message_type}]"
    return "[Mensaje recibido sin texto por Kommo]"


def _local_sender_id(job: dict) -> str:
    return str(job.get("chat_id") or job.get("contact_id") or job.get("lead_id") or job.get("correlation_id"))


def _customer_profile_from_lead(job: dict, lead: dict | None) -> dict:
    profile = {"display_name": None}
    if lead and lead.get("name"):
        profile["display_name"] = lead.get("name")
    if job.get("channel") == "whatsapp" and job.get("contact_id"):
        profile["phone"] = None
    if job.get("channel") == "instagram":
        profile["instagram_handle"] = None
    return profile
