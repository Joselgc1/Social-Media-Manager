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
from app.integrations.kommo.text_sanitizer import (
    build_kommo_message_diagnostics,
    prepare_kommo_customer_message,
)
from app.webhooks.inbound_buffer import MESSAGE_DEBOUNCE_SECONDS

logger = logging.getLogger(__name__)

MAX_JOB_ATTEMPTS = 3
STALE_PROCESSING_MINUTES = 5
STALE_WAITING_MINUTES = 3
_TERMINAL_STATUSES = {"sent", "discarded", "failed", "delivery_unknown"}
_ACTIVE_SALESBOT_STATUSES = {"prepared", "waiting_for_salesbot", "ready", "processing", "continuing"}
_TRANSIENT_CONTINUATION_STATUSES = {429, 500, 502, 503, 504}


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
            """
            SELECT job_id, receipt_status
            FROM kommo_message_receipts
            WHERE external_message_id = :external_message_id
            """,
            {"external_message_id": external_message_id},
        )
        if existing:
            return {"status": "duplicate", "job_id": str(existing["job_id"])}

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
            await _record_message_receipt(event, external_message_id, merged_job_id, "merged")
            return {"status": "merged", "job_id": merged_job_id}

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
        await _record_message_receipt(event, external_message_id, str(job_id), "created")
        return {"status": "created", "job_id": str(job_id)}


async def schedule_due_job_processing(delay_seconds: float | None = None) -> None:
    await asyncio.sleep(delay_seconds if delay_seconds is not None else MESSAGE_DEBOUNCE_SECONDS + 0.2)
    try:
        processed = await process_pending_jobs(limit=5)
        logger.info("Kommo pending job processor completed: processed=%s", processed)
    except Exception as e:
        logger.exception("Kommo pending job processor failed: %s", sanitize_job_error(e))


async def process_pending_jobs(limit: int = 10) -> int:
    if get_config().channel_backend != "kommo":
        return 0
    processed = 0
    for _ in range(limit):
        job = await _claim_due_pending_job()
        if not job:
            if processed == 0:
                await _log_pending_claim_diagnostics()
            break
        job_dict = dict(job)
        logger.info("Kommo pending job claimed: %s", _job_log_context(job_dict))
        await _launch_salesbot_for_job(job_dict)
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
        job_dict = dict(job)
        logger.info("Kommo ready job claimed: %s", _job_log_context(job_dict))
        await _process_ready_job(job_dict)
        processed += 1
    return processed


async def persist_salesbot_callback(data: SalesbotWidgetData, return_url: str, claims: dict | None = None) -> dict:
    values = _callback_values(data, return_url, claims or {})
    update_values = {
        "return_url": values["return_url"],
        "entity_id": values["entity_id"],
        "entity_type": values["entity_type"],
        "callback_claims": values["callback_claims"],
        "salesbot_token_jti": values["salesbot_token_jti"],
        "salesbot_account_id": values["salesbot_account_id"],
        "salesbot_user_id": values["salesbot_user_id"],
        "salesbot_client_uuid": values["salesbot_client_uuid"],
    }

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
                  (:entity_type = 'leads' AND candidate.lead_id = :entity_id)
                  OR (:entity_type = 'contacts' AND candidate.contact_id = :entity_id)
              )
            ORDER BY candidate.salesbot_launched_at DESC NULLS LAST, candidate.created_at DESC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING *
        """,
        update_values,
    )
    if job:
        logger.info("Kommo Salesbot callback matched waiting job: %s", _job_log_context(dict(job)))
        return {"status": "ready", "job_id": str(job["id"])}

    latest = await _find_latest_job_for_callback(values)
    if latest and latest["status"] in _TERMINAL_STATUSES | _ACTIVE_SALESBOT_STATUSES:
        logger.info("Kommo Salesbot callback ignored as duplicate for job: %s", _job_log_context(dict(latest)))
        return {"status": "duplicate", "job_id": str(latest["id"])}
    logger.info(
        "Kommo Salesbot callback ignored: no_waiting_job entity_type=%s entity_id=%s",
        values["entity_type"],
        values["entity_id"],
    )
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
          AND COALESCE(salesbot_launched_at, updated_at, created_at) < NOW() - (:minutes * INTERVAL '1 minute')
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
    if any(_affected_rows(value) for value in (failed_waiting, reset_processing, marked_delivery_unknown, marked_failed)):
        logger.info(
            "Kommo stale job recovery completed: failed_waiting=%s reset_processing=%s marked_delivery_unknown=%s marked_failed=%s",
            failed_waiting,
            reset_processing,
            marked_delivery_unknown,
            marked_failed,
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
            (SELECT MAX(COALESCE(received_at, created_at)) FROM kommo_message_receipts) AS last_incoming,
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


async def _log_pending_claim_diagnostics() -> None:
    pending = await db.fetch_one(
        """
        SELECT id, correlation_id, status, channel, origin, lead_id, contact_id, chat_id, talk_id,
               combined_message, media_url, buffer_expires_at,
               GREATEST(EXTRACT(EPOCH FROM (buffer_expires_at - NOW())), 0) AS seconds_until_due
        FROM kommo_message_jobs
        WHERE status = 'pending'
        ORDER BY created_at ASC
        LIMIT 1
        """
    )
    if not pending:
        return

    blockers = await db.fetch_all(
        """
        SELECT id, status, channel, origin, lead_id, contact_id, chat_id, talk_id,
               combined_message, media_url, return_url, attempt_count, buffer_expires_at,
               salesbot_launched_at, processing_started_at, created_at, updated_at, last_error
        FROM kommo_message_jobs
        WHERE correlation_id = :correlation_id
          AND status IN ('prepared', 'waiting_for_salesbot', 'ready', 'processing', 'continuing')
          AND id <> :id
        ORDER BY updated_at DESC
        LIMIT 3
        """,
        {"correlation_id": pending["correlation_id"], "id": pending["id"]},
    )
    logger.info(
        "Kommo pending claim skipped: pending=%s blockers=%s",
        _job_log_context(dict(pending)),
        [_job_log_context(dict(blocker)) | {"last_error": sanitize_job_error(blocker["last_error"]) if blocker["last_error"] else None} for blocker in blockers],
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
        logger.info("Kommo Salesbot launch preparing job: %s", _job_log_context(job))
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
            logger.info(
                "Kommo job suppressed before Salesbot launch: job_id=%s reason=%s",
                job["id"],
                decision.reason,
            )
            await _store_user_message_if_suppressed(customer, job)
            await _mark_job(job["id"], "discarded" if not decision.needs_ai_mode_initialization else "failed", decision.reason)
            return

        entity_id = job.get("lead_id") or job.get("contact_id")
        entity_type = "leads" if job.get("lead_id") else "contacts"
        if not entity_id:
            logger.warning("Kommo job failed before Salesbot launch: job_id=%s reason=missing_entity_id", job["id"])
            await _mark_job(job["id"], "failed", "missing_entity_id")
            return
        waiting_job = await _mark_job_waiting_for_salesbot(job["id"])
        if not waiting_job:
            logger.info("Kommo Salesbot launch skipped because job was no longer processing: job_id=%s", job["id"])
            return
        logger.info("Kommo job waiting for Salesbot callback before launch: job_id=%s", job["id"])
        try:
            await client.run_salesbot(entity_id, entity_type)
        except KommoAPIError as e:
            error = sanitize_job_error(e)
            if _is_definitive_launch_error(e):
                logger.warning("Kommo Salesbot launch rejected definitively: job_id=%s error=%s", job["id"], error)
                await _mark_waiting_job_failed(job["id"], error)
                return
            logger.warning("Kommo Salesbot launch outcome uncertain: job_id=%s error=%s", job["id"], error)
            await _record_uncertain_launch_warning(job["id"], error)
            return
        except Exception as e:
            error = sanitize_job_error(e)
            logger.warning("Kommo Salesbot launch outcome uncertain: job_id=%s error=%s", job["id"], error)
            await _record_uncertain_launch_warning(job["id"], error)
            return
        logger.info("Kommo Salesbot launch accepted for job_id=%s", job["id"])
    except Exception as e:
        logger.exception("Kommo Salesbot launch failed: job_id=%s error=%s", job["id"], sanitize_job_error(e))
        await _mark_job(job["id"], "failed", sanitize_job_error(e))


async def _mark_job_waiting_for_salesbot(job_id: str):
    return await db.fetch_one(
        """
        UPDATE kommo_message_jobs
        SET status = 'waiting_for_salesbot',
            salesbot_launched_at = NOW(),
            processing_started_at = NULL,
            updated_at = NOW()
        WHERE id = :id
          AND status = 'processing'
        RETURNING *
        """,
        {"id": job_id},
    )


def _is_definitive_launch_error(error: KommoAPIError) -> bool:
    return error.status_code is not None and 400 <= error.status_code < 500 and error.status_code != 429


async def _mark_waiting_job_failed(job_id: str, error: str) -> None:
    await db.execute(
        """
        UPDATE kommo_message_jobs
        SET status = 'failed',
            last_error = :last_error,
            processing_started_at = NULL,
            completed_at = NOW(),
            updated_at = NOW()
        WHERE id = :id
          AND status = 'waiting_for_salesbot'
        """,
        {"id": job_id, "last_error": sanitize_job_error(error)},
    )


async def _record_uncertain_launch_warning(job_id: str, error: str) -> None:
    current = await db.fetch_one("SELECT status FROM kommo_message_jobs WHERE id = :id", {"id": job_id})
    if current and current["status"] == "ready":
        logger.info("Kommo Salesbot callback already arrived after uncertain launch: job_id=%s", job_id)
        return
    if not current or current["status"] != "waiting_for_salesbot":
        logger.info("Kommo launch warning skipped because job status changed: job_id=%s", job_id)
        return
    await db.execute(
        """
        UPDATE kommo_message_jobs
        SET last_error = :last_error,
            updated_at = NOW()
        WHERE id = :id
          AND status = 'waiting_for_salesbot'
        """,
        {"id": job_id, "last_error": f"Salesbot launch outcome uncertain: {sanitize_job_error(error)}"},
    )


async def _process_ready_job(job: dict) -> None:
    config = get_config()
    client = KommoClient.from_config()
    continuation_started = False
    try:
        logger.info("Kommo ready job processing started: %s", _job_log_context(job))
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
            logger.info("Kommo ready job suppressed before AI response: job_id=%s reason=%s", job["id"], before.reason)
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
                logger.info("Kommo ready job suppressed after AI response: job_id=%s reason=%s", job["id"], after.reason)
                await _continue_and_discard_job(client, job, after.reason)
                return

        mapped = map_ai_response_to_salesbot(result)
        raw_customer_text = (mapped.customer_text or "").strip()
        if not raw_customer_text:
            logger.info(
                "Kommo ready job produced no deliverable reply: job_id=%s reason=%s",
                job["id"],
                mapped.reason or "empty_response",
            )
            await _continue_and_discard_job(client, job, mapped.reason or "empty_response")
            return
        customer_text, message_diagnostics = prepare_kommo_customer_message(
            raw_customer_text,
            job.get("channel") or "whatsapp",
            settings,
        )
        if not customer_text:
            logger.info(
                "Kommo ready job produced no deliverable reply after transport sanitization: job_id=%s reason=empty_after_sanitization",
                job["id"],
            )
            await _continue_and_discard_job(client, job, "empty_after_sanitization")
            return
        continuation_data = {"status": "success", "message": customer_text}
        await _mark_job_continuing(job["id"], continuation_data)
        continuation_started = True
        _log_continuation_prepared(job["id"], continuation_data, message_diagnostics)
        response_payload = await client.continue_salesbot(
            job["return_url"],
            data=continuation_data,
        )
        await _store_assistant_message_after_delivery(customer, job, result, customer_text)
        await _mark_job_sent(job["id"], response_payload)
    except KommoAPIError as e:
        logger.warning("Kommo ready job API error: job_id=%s error=%s", job["id"], sanitize_job_error(e))
        if not continuation_started and job.get("return_url"):
            await _continue_and_discard_job(client, job, sanitize_job_error(e))
            return
        await _mark_job(job["id"], _status_after_continuation_error(e, continuation_started), sanitize_job_error(e))
    except Exception as e:
        logger.exception("Kommo ready job failed: job_id=%s error=%s", job["id"], sanitize_job_error(e))
        if not continuation_started and job.get("return_url"):
            await _continue_and_discard_job(client, job, sanitize_job_error(e))
            return
        await _mark_job(job["id"], "delivery_unknown" if continuation_started else "failed", sanitize_job_error(e))


async def _find_latest_job_for_callback(values: dict):
    query_values = {
        "entity_type": values["entity_type"],
        "entity_id": values["entity_id"],
    }
    query = """
        SELECT * FROM kommo_message_jobs
        WHERE (
            (:entity_type = 'leads' AND lead_id = :entity_id)
            OR (:entity_type = 'contacts' AND contact_id = :entity_id)
        )
        ORDER BY salesbot_launched_at DESC NULLS LAST, created_at DESC
        LIMIT 1
    """
    return await db.fetch_one(query, query_values)


def _callback_values(data: SalesbotWidgetData, return_url: str, claims: dict) -> dict:
    entity_type = claims.get("entity_type")
    entity_id = _claim_as_str(claims, "entity_id")
    if entity_type not in {"leads", "contacts"} or not entity_id:
        raise ValueError("missing_signed_entity_identity")
    if entity_type == "leads" and data.lead_id and data.lead_id != entity_id:
        raise ValueError("widget_lead_id_mismatch")
    if entity_type == "contacts" and data.contact_id and data.contact_id != entity_id:
        raise ValueError("widget_contact_id_mismatch")
    return {
        "return_url": return_url,
        "entity_id": entity_id,
        "entity_type": entity_type,
        "callback_claims": json.dumps(_safe_claims(claims)),
        "salesbot_token_jti": _claim_as_str(claims, "jti"),
        "salesbot_account_id": _claim_as_str(claims, "account_id"),
        "salesbot_user_id": _claim_as_str(claims, "user_id"),
        "salesbot_client_uuid": _claim_as_str(claims, "client_uid") or _claim_as_str(claims, "client_uuid"),
    }


def _safe_claims(claims: dict) -> dict:
    allowed = {"iss", "aud", "jti", "iat", "nbf", "exp", "account_id", "user_id", "subdomain", "client_uuid", "client_uid", "entity_id", "entity_type"}
    return {key: claims[key] for key in allowed if key in claims}


def _claim_as_str(claims: dict, key: str) -> str | None:
    value = claims.get(key)
    return str(value) if value not in (None, "") else None


async def _continue_and_discard_job(client: KommoClient, job: dict, reason: str | None) -> None:
    continuation_started = False
    try:
        continuation_data = {"status": "fail", "message": ""}
        logger.info("Kommo continuing Salesbot with failure status: job_id=%s reason=%s", job["id"], reason)
        await _mark_job_continuing(job["id"], continuation_data)
        continuation_started = True
        _log_continuation_prepared(
            job["id"],
            continuation_data,
            build_kommo_message_diagnostics("", job.get("channel") or "whatsapp", "preserve"),
        )
        response_payload = await client.continue_salesbot(
            job["return_url"],
            data=continuation_data,
        )
        await _mark_job_discarded(job["id"], reason, response_payload)
    except KommoAPIError as e:
        status = _status_after_continuation_error(e, continuation_started)
        logger.warning(
            "Kommo failure continuation rejected: job_id=%s status=%s error=%s",
            job["id"],
            status,
            sanitize_job_error(e),
        )
        await _mark_job(job["id"], status, sanitize_job_error(e))
    except Exception as e:
        await _mark_job(job["id"], "delivery_unknown" if continuation_started else "failed", sanitize_job_error(e))


async def _mark_job_continuing(
    job_id: str,
    continuation_data: dict,
) -> None:
    continuation_payload = {"data": continuation_data}
    await db.execute(
        """
        UPDATE kommo_message_jobs
        SET status = 'continuing',
            continuation_payload = CAST(:continuation_payload AS jsonb),
            processing_started_at = NOW(),
            updated_at = NOW()
        WHERE id = :id AND status = 'processing'
        """,
        {
            "id": job_id,
            "continuation_payload": json.dumps(continuation_payload),
        },
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
    logger.info("Kommo continuation accepted: job_id=%s", job_id)


def _log_continuation_prepared(
    job_id: str,
    continuation_data: dict,
    message_diagnostics: dict | None = None,
) -> None:
    message = str(continuation_data.get("message") or "")
    diagnostics = message_diagnostics or build_kommo_message_diagnostics(message, None, False)
    logger.info(
        "Kommo continuation prepared: job_id=%s status=%s channel=%s message_present=%s message_length=%s newline_count=%s non_ascii_present=%s emoji_present=%s replacement_char_present=%s literal_question_mark_present=%s kommo_emoji_mode=%s kommo_strip_emoji_applied=%s",
        job_id,
        continuation_data.get("status"),
        diagnostics["channel"],
        bool(message),
        diagnostics["message_length"],
        diagnostics["newline_count"],
        diagnostics["non_ascii_present"],
        diagnostics["emoji_present"],
        diagnostics["replacement_char_present"],
        diagnostics["literal_question_mark_present"],
        diagnostics["kommo_emoji_mode"],
        diagnostics["kommo_strip_emoji_applied"],
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
    logger.info("Kommo job marked discarded: job_id=%s reason=%s", job_id, reason)


async def _store_assistant_message_after_delivery(customer: dict, job: dict, result: dict, customer_text: str | None) -> None:
    content = customer_text or result.get("text")
    if not content:
        return
    async with db.get_db().transaction():
        existing = await db.fetch_one(
            """
            SELECT assistant_message_persisted_at
            FROM kommo_message_jobs
            WHERE id = :id
            FOR UPDATE
            """,
            {"id": job["id"]},
        )
        if existing and existing["assistant_message_persisted_at"]:
            logger.info("Kommo assistant history already persisted: job_id=%s", job["id"])
            return
        await conversations.store_message(
            customer_id=customer["id"],
            role="assistant",
            content=content,
            channel=job.get("channel") or customer.get("channel") or "whatsapp",
            function_calls=result.get("function_calls"),
        )
        await db.execute(
            """
            UPDATE kommo_message_jobs
            SET assistant_message_persisted_at = NOW(),
                updated_at = NOW()
            WHERE id = :id
              AND assistant_message_persisted_at IS NULL
            """,
            {"id": job["id"]},
        )
    logger.info("Kommo assistant history persisted: job_id=%s", job["id"])


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
    logger.info("Kommo job marked %s: job_id=%s reason=%s", status, job_id, sanitize_job_error(error) if error else None)


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


async def _record_message_receipt(
    event: NormalizedKommoEvent,
    external_message_id: str,
    job_id: str,
    receipt_status: str,
) -> None:
    await db.execute(
        """
        INSERT INTO kommo_message_receipts (
            external_message_id, job_id, correlation_id, lead_id, contact_id, chat_id,
            talk_id, origin, channel, receipt_status, received_at
        ) VALUES (
            :external_message_id, :job_id, :correlation_id, :lead_id, :contact_id, :chat_id,
            :talk_id, :origin, :channel, :receipt_status, :received_at
        )
        ON CONFLICT (external_message_id) DO NOTHING
        """,
        {
            "external_message_id": external_message_id,
            "job_id": job_id,
            "correlation_id": event.correlation_id,
            "lead_id": event.lead_id,
            "contact_id": event.contact_id,
            "chat_id": event.chat_id,
            "talk_id": event.talk_id,
            "origin": event.origin,
            "channel": event.channel,
            "receipt_status": receipt_status,
            "received_at": event.created_at,
        },
    )


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


def _job_log_context(job: dict) -> dict:
    return {
        "job_id": str(job.get("id")) if job.get("id") is not None else None,
        "status": job.get("status"),
        "channel": job.get("channel"),
        "origin": job.get("origin"),
        "lead_id": job.get("lead_id"),
        "contact_id": job.get("contact_id"),
        "chat_id": job.get("chat_id"),
        "talk_id": job.get("talk_id"),
        "has_message": bool(job.get("combined_message")),
        "has_media": bool(job.get("media_url")),
        "has_return_url": bool(job.get("return_url")),
        "attempt_count": job.get("attempt_count"),
        "seconds_until_due": float(job["seconds_until_due"]) if job.get("seconds_until_due") is not None else None,
        "salesbot_launched_at": _timestamp_for_log(job.get("salesbot_launched_at")),
        "processing_started_at": _timestamp_for_log(job.get("processing_started_at")),
        "created_at": _timestamp_for_log(job.get("created_at")),
        "updated_at": _timestamp_for_log(job.get("updated_at")),
    }


def _timestamp_for_log(value) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _affected_rows(value) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.rsplit(" ", 1)[-1])
        except ValueError:
            return 0
    return 0
