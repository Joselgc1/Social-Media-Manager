"""Durable PostgreSQL-backed Kommo job processing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime

from app import db
from app.ai.engine import generate_response
from app.ai.transcription import AudioTranscriptionError, transcribe_audio_url
from app.config import get_config
from app.crm import conversations, escalations, sessions
from app.crm.channel_mappings import (
    persist_verified_meta_instagram_sender,
    resolve_customer_from_kommo_job,
    upsert_mapping,
)
from app.integrations.kommo.client import KommoAPIError, KommoClient, sanitize_kommo_error
from app.integrations.kommo.customer_profile import build_kommo_customer_profile
from app.integrations.kommo.delivery import (
    KommoDeliveryAbortedError,
    KommoDeliveryStateError,
    KommoDeliveryUnknownError,
    KommoPartialDeliveryError,
    deliver_response,
    get_enabled_media_types,
)
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
from app.integrations.meta_context.normalization import (
    normalize_message_text,
    normalized_text_hash,
)
from app.webhooks.inbound_buffer import MESSAGE_DEBOUNCE_SECONDS

logger = logging.getLogger(__name__)

MAX_JOB_ATTEMPTS = 3
STALE_PROCESSING_MINUTES = 5
STALE_WAITING_MINUTES = 3
DELIVERY_RECONCILE_INTERVAL_SECONDS = 15
CUSTOMER_DELIVERY_FAILURE_MESSAGE = (
    "Disculpa, tuve un problema al enviarte el archivo. "
    "Por favor inténtalo de nuevo en unos minutos."
)
_TERMINAL_STATUSES = {"sent", "discarded", "failed", "delivery_unknown"}
_ACTIVE_SALESBOT_STATUSES = {
    "prepared",
    "waiting_for_salesbot",
    "waiting_for_context",
    "waiting_for_delivery",
    "ready",
    "processing",
    "continuing",
}
_TRANSIENT_CONTINUATION_STATUSES = {429, 500, 502, 503, 504}
_AUDIO_MESSAGE_TYPES = {"voice", "audio"}
_IMAGE_MESSAGE_TYPES = {"image", "picture"}
UNSUPPORTED_ATTACHMENT_MESSAGE = (
    "No pude identificar el archivo que me enviaste. "
    "Por favor envíame una imagen del comprobante o escríbeme qué necesitas."
)
COMMENT_MIRROR_RECONCILIATION_SECONDS = 45
COMMENT_MIRROR_AMBIGUITY_SECONDS = 1.0
COMMENT_MIRROR_FALLBACK_MAX_DELTA_SECONDS = 5.0
COMMENT_CALLBACK_DEDUP_SECONDS = 300
COMMENT_PRIVATE_SUPERSEDED_REASON = "superseded_by_instagram_comment"
_PUBLIC_COMMENT_CONTEXT_FIELDS = {
    "post_id",
    "comment_id",
    "parent_comment_id",
    "media_id",
    "post_url",
    "comment_url",
    "post_caption",
    "media_type",
    "media_product_type",
    "content_type",
    "post_text",
    "media_caption",
    "product_name",
    "post_product_name",
    "product_sku",
    "parent_sku",
    "image_url",
    "post_image_url",
    "post_media_url",
}


def sanitize_job_error(error: Exception | str) -> str:
    return sanitize_kommo_error(error)


async def record_incoming_event(event: NormalizedKommoEvent) -> dict:
    """Persist an inbound message and merge it into any still-pending debounce job."""
    external_message_id = event.message_id or f"{event.correlation_id}:{event.created_at or datetime.now(UTC)}"
    text = (event.text or "").strip()
    if _is_audio_message_type(event.message_type) or not text:
        text = _message_placeholder(event, external_message_id)
    inbound_attachments = _event_inbound_attachments(event, external_message_id)

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
            SELECT id, combined_message, media_url, message_type, inbound_attachments
            FROM kommo_message_jobs
            WHERE correlation_id = :correlation_id
              AND interaction_type = :interaction_type
              AND status = 'pending'
            ORDER BY created_at DESC
            FOR UPDATE
            LIMIT 1
            """,
            {"correlation_id": event.correlation_id, "interaction_type": event.interaction_type},
        )
        if pending:
            merged = "\n".join(part for part in (pending["combined_message"], text) if part)
            merged_media_url, merged_message_type = _merged_legacy_media(
                pending["media_url"],
                pending["message_type"],
                event,
            )
            await db.execute(
                """
                UPDATE kommo_message_jobs
                SET combined_message = :combined_message,
                    media_url = :media_url,
                    message_type = :message_type,
                    inbound_attachments = CASE
                        WHEN CAST(:inbound_attachments AS jsonb) = '[]'::jsonb THEN inbound_attachments
                        WHEN EXISTS (
                            SELECT 1
                            FROM jsonb_array_elements(inbound_attachments) AS attachment
                            WHERE attachment ->> 'external_message_id' = :external_message_id
                        ) THEN inbound_attachments
                        ELSE inbound_attachments || CAST(:inbound_attachments AS jsonb)
                    END,
                    lead_id = COALESCE(lead_id, :lead_id),
                    contact_id = COALESCE(contact_id, :contact_id),
                    chat_id = COALESCE(chat_id, :chat_id),
                    talk_id = COALESCE(talk_id, :talk_id),
                    author_id = COALESCE(:author_id, author_id),
                    author_name = COALESCE(:author_name, author_name),
                    author_username = COALESCE(:author_username, author_username),
                    author_profile_url = COALESCE(:author_profile_url, author_profile_url),
                    sender_username = COALESCE(:sender_username, sender_username),
                    sender_profile_url = COALESCE(:sender_profile_url, sender_profile_url),
                    origin = COALESCE(origin, :origin),
                    channel = COALESCE(channel, :channel),
                    interaction_type = :interaction_type,
                    updated_at = NOW(),
                    buffer_expires_at = NOW() + (:debounce_seconds * INTERVAL '1 second')
                WHERE id = :id
                """,
                {
                    "combined_message": merged,
                    "media_url": merged_media_url,
                    "message_type": merged_message_type,
                    "inbound_attachments": json.dumps(inbound_attachments),
                    "external_message_id": external_message_id,
                    "lead_id": event.lead_id,
                    "contact_id": event.contact_id,
                    "chat_id": event.chat_id,
                    "talk_id": event.talk_id,
                    "author_id": event.author_id,
                    "author_name": event.author_name,
                    "author_username": event.author_username,
                    "author_profile_url": event.author_profile_url,
                    "sender_username": event.sender_username,
                    "sender_profile_url": event.sender_profile_url,
                    "origin": event.origin,
                    "channel": event.channel,
                    "interaction_type": event.interaction_type,
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
                author_id, author_name, author_username, author_profile_url, sender_username, sender_profile_url,
                origin, channel, interaction_type, combined_message, media_url, message_type,
                inbound_attachments,
                status, buffer_expires_at
            ) VALUES (
                :correlation_id, :external_message_id, :lead_id, :contact_id, :chat_id, :talk_id,
                :author_id, :author_name, :author_username, :author_profile_url, :sender_username, :sender_profile_url,
                :origin, :channel, :interaction_type, :combined_message, :media_url, :message_type,
                CAST(:inbound_attachments AS jsonb),
                'pending', NOW() + (:debounce_seconds * INTERVAL '1 second')
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
        if _is_direct_instagram_dm(job_dict):
            await _prepare_direct_instagram_dm(job_dict)
        else:
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


async def process_waiting_for_delivery_jobs(limit: int = 10) -> int:
    """Reconcile accepted direct Instagram messages without ever resending them."""
    if get_config().channel_backend != "kommo":
        return 0
    processed = 0
    for _ in range(limit):
        job = await _claim_waiting_for_delivery_job()
        if not job:
            break
        await _reconcile_waiting_for_delivery_job(dict(job))
        processed += 1
    return processed


async def request_delivery_reconciliation(provider_message_id: str | None) -> bool:
    """Make a known direct Instagram delivery eligible for scheduler reconciliation."""
    message_id = str(provider_message_id or "").strip()
    if not message_id:
        return False
    updated = await db.fetch_one(
        """
        UPDATE kommo_message_jobs job
        SET delivery_reconcile_after_at = NOW(),
            updated_at = NOW()
        FROM kommo_outbound_deliveries outbound
        WHERE outbound.job_id = job.id
          AND outbound.transport = 'chats_api'
          AND outbound.provider_message_id = :provider_message_id
          AND job.status = 'waiting_for_delivery'
          AND job.channel = 'instagram'
          AND job.interaction_type = 'private_message'
        RETURNING job.id
        """,
        {"provider_message_id": message_id},
    )
    return bool(updated)


async def _claim_waiting_for_delivery_job():
    return await db.fetch_one(
        """
        UPDATE kommo_message_jobs job
        SET delivery_reconcile_after_at = NOW() + (:interval_seconds * INTERVAL '1 second'),
            delivery_reconcile_attempt_count = delivery_reconcile_attempt_count + 1,
            updated_at = NOW()
        WHERE job.id = (
            SELECT candidate.id
            FROM kommo_message_jobs candidate
            WHERE candidate.status = 'waiting_for_delivery'
              AND candidate.channel = 'instagram'
              AND candidate.interaction_type = 'private_message'
              AND candidate.delivery_reconcile_after_at <= NOW()
            ORDER BY candidate.delivery_reconcile_after_at, candidate.created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING job.*
        """,
        {"interval_seconds": DELIVERY_RECONCILE_INTERVAL_SECONDS},
    )


async def _reconcile_waiting_for_delivery_job(job: dict) -> None:
    outbound_rows = await db.fetch_all(
        """
        SELECT id, status, provider_message_id
        FROM kommo_outbound_deliveries
        WHERE job_id = CAST(:job_id AS uuid)
          AND transport = 'chats_api'
        ORDER BY created_at
        """,
        {"job_id": job["id"]},
    )
    if not outbound_rows:
        logger.warning(
            "Kommo delivery reconciliation deferred: job_id=%s talk_id=%s "
            "provider_message_id=missing provider_delivery_status=missing_outbound",
            job["id"],
            job.get("talk_id"),
        )
        return

    client = KommoClient.from_config()
    all_delivered = True
    for outbound_record in outbound_rows:
        outbound = dict(outbound_record)
        provider_message_id = str(outbound.get("provider_message_id") or "").strip()
        if outbound.get("status") == "confirmed":
            continue
        if outbound.get("status") == "failed":
            await _fail_waiting_delivery(
                job,
                provider_message_id,
                "error",
            )
            return
        if outbound.get("status") != "accepted" or not provider_message_id:
            all_delivered = False
            continue
        try:
            provider_status = await client.get_talk_message_delivery_status(
                str(job.get("talk_id") or ""),
                provider_message_id,
            )
        except Exception as error:
            logger.warning(
                "Kommo delivery reconciliation lookup failed: job_id=%s talk_id=%s "
                "provider_message_id=%s provider_delivery_status=lookup_failed error=%s",
                job["id"],
                job.get("talk_id"),
                provider_message_id,
                sanitize_job_error(error),
            )
            return

        logger.info(
            "Kommo delivery reconciliation observed: job_id=%s talk_id=%s "
            "provider_message_id=%s provider_delivery_status=%s",
            job["id"],
            job.get("talk_id"),
            provider_message_id,
            provider_status or "missing",
        )
        if provider_status in {"delivered", "seen"}:
            await _confirm_waiting_delivery(outbound["id"], provider_message_id)
            continue
        if provider_status == "error":
            await _fail_waiting_delivery(job, provider_message_id, provider_status)
            return
        all_delivered = False

    if all_delivered:
        await _finalize_waiting_delivery(job["id"])


async def _confirm_waiting_delivery(outbound_id, provider_message_id: str) -> None:
    await db.execute(
        """
        UPDATE kommo_outbound_deliveries
        SET status = 'confirmed',
            confirmed_at = COALESCE(confirmed_at, NOW()),
            last_error = NULL,
            updated_at = NOW()
        WHERE id = :id
          AND provider_message_id = :provider_message_id
          AND status = 'accepted'
        """,
        {"id": outbound_id, "provider_message_id": provider_message_id},
    )


async def _fail_waiting_delivery(
    job: dict,
    provider_message_id: str,
    provider_status: str,
) -> None:
    diagnostic = sanitize_job_error(
        f"Kommo provider delivery_status={provider_status} for message {provider_message_id}"
    )
    async with db.get_db().transaction():
        await db.execute(
            """
            UPDATE kommo_outbound_deliveries
            SET status = 'failed',
                last_error = :last_error,
                updated_at = NOW()
            WHERE job_id = CAST(:job_id AS uuid)
              AND transport = 'chats_api'
              AND provider_message_id = :provider_message_id
              AND status IN ('accepted', 'failed')
            """,
            {
                "job_id": job["id"],
                "provider_message_id": provider_message_id,
                "last_error": diagnostic,
            },
        )
        await db.execute(
            """
            UPDATE kommo_message_jobs
            SET status = 'failed',
                last_error = :last_error,
                pending_assistant_message = NULL,
                delivery_reconcile_after_at = NULL,
                completed_at = NOW(),
                updated_at = NOW()
            WHERE id = :id
              AND status = 'waiting_for_delivery'
            """,
            {"id": job["id"], "last_error": diagnostic},
        )
    logger.warning(
        "Kommo direct Instagram delivery failed: job_id=%s talk_id=%s "
        "provider_message_id=%s provider_delivery_status=%s",
        job["id"],
        job.get("talk_id"),
        provider_message_id,
        provider_status,
    )


async def _finalize_waiting_delivery(job_id: str) -> None:
    job_record = await db.fetch_one(
        """
        SELECT * FROM kommo_message_jobs
        WHERE id = :id AND status = 'waiting_for_delivery'
        """,
        {"id": job_id},
    )
    if not job_record:
        return
    job = dict(job_record)
    payload = _json_dict(job.get("pending_assistant_message"))
    await _persist_waiting_assistant_message(job, payload)
    final_status = payload.get("final_status")
    if final_status not in {"sent", "discarded"}:
        final_status = "sent"
    updated = await db.fetch_one(
        """
        UPDATE kommo_message_jobs
        SET status = :final_status,
            last_error = CASE WHEN :final_status = 'sent' THEN NULL ELSE last_error END,
            pending_assistant_message = NULL,
            delivery_reconcile_after_at = NULL,
            completed_at = NOW(),
            updated_at = NOW()
        WHERE id = :id
          AND status = 'waiting_for_delivery'
          AND (
              assistant_message_persisted_at IS NOT NULL
              OR pending_assistant_message IS NULL
              OR COALESCE(pending_assistant_message->>'customer_id', '') = ''
          )
        RETURNING id, talk_id
        """,
        {"id": job_id, "final_status": final_status},
    )
    if updated:
        provider_ids = _provider_message_ids(job)
        logger.info(
            "Kommo direct Instagram delivery finalized: job_id=%s talk_id=%s "
            "provider_message_id=%s provider_delivery_status=delivered",
            job_id,
            updated["talk_id"],
            ",".join(provider_ids),
        )


async def _persist_waiting_assistant_message(job: dict, payload: dict) -> None:
    customer_id = str(payload.get("customer_id") or "").strip()
    content = str(payload.get("content") or "")
    attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
    if not customer_id or (not content and not attachments):
        return
    async with db.get_db().transaction():
        locked = await db.fetch_one(
            """
            SELECT assistant_message_persisted_at
            FROM kommo_message_jobs
            WHERE id = :id AND status = 'waiting_for_delivery'
            FOR UPDATE
            """,
            {"id": job["id"]},
        )
        if not locked or locked["assistant_message_persisted_at"]:
            return
        await conversations.store_message(
            customer_id=customer_id,
            role="assistant",
            content=content,
            channel=str(payload.get("channel") or "instagram"),
            function_calls=payload.get("function_calls"),
            source_id=_conversation_source_id(job),
            attachments=attachments or None,
            interaction_type="private_message",
        )
        await db.execute(
            """
            UPDATE kommo_message_jobs
            SET assistant_message_persisted_at = NOW(), updated_at = NOW()
            WHERE id = :id
              AND status = 'waiting_for_delivery'
              AND assistant_message_persisted_at IS NULL
            """,
            {"id": job["id"]},
        )


def _json_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _provider_message_ids(job: dict) -> list[str]:
    response = _json_dict(job.get("continuation_response"))
    values = response.get("provider_message_ids")
    return [str(value) for value in values] if isinstance(values, list) else []


async def persist_salesbot_callback(data: SalesbotWidgetData, return_url: str, claims: dict | None = None) -> dict:
    values = _callback_values(data, return_url, claims or {})
    update_values = {
        "return_url": values["return_url"],
        "entity_id": values["entity_id"],
        "entity_type": values["entity_type"],
        "widget_contact_id": values["widget_contact_id"],
        "callback_claims": values["callback_claims"],
        "salesbot_token_jti": values["salesbot_token_jti"],
        "salesbot_token_iat": values["salesbot_token_iat"],
        "salesbot_token_iat_text": values["salesbot_token_iat_text"],
        "salesbot_account_id": values["salesbot_account_id"],
        "salesbot_user_id": values["salesbot_user_id"],
        "salesbot_client_uuid": values["salesbot_client_uuid"],
        "interaction_type": values["interaction_type"],
        "expected_channel": values["expected_channel"],
        "author_username": values["author_username"],
        "author_profile_url": values["author_profile_url"],
        "sender_username": values["sender_username"],
        "sender_profile_url": values["sender_profile_url"],
    }

    job = await db.fetch_one(
        """
        UPDATE kommo_message_jobs job
        SET return_url = :return_url,
            status = 'ready',
            processing_lease_id = NULL,
            ai_started_at = NULL,
            callback_claims = CAST(:callback_claims AS jsonb),
            salesbot_token_jti = :salesbot_token_jti,
            salesbot_account_id = :salesbot_account_id,
            salesbot_user_id = :salesbot_user_id,
            salesbot_client_uuid = :salesbot_client_uuid,
            author_username = COALESCE(:author_username, author_username),
            author_profile_url = COALESCE(:author_profile_url, author_profile_url),
            sender_username = COALESCE(:sender_username, sender_username),
            sender_profile_url = COALESCE(:sender_profile_url, sender_profile_url),
            updated_at = NOW()
        WHERE job.id = (
            SELECT candidate.id
            FROM kommo_message_jobs candidate
            WHERE candidate.status = 'waiting_for_salesbot'
               AND candidate.salesbot_launched_at < to_timestamp(:salesbot_token_iat + 1)
              AND NOT EXISTS (
                  SELECT 1
                  FROM kommo_message_jobs consumed
                   WHERE (
                          consumed.return_url = :return_url
                          AND consumed.callback_claims ->> 'iat' = :salesbot_token_iat_text
                         )
                      OR (
                          CAST(:salesbot_token_jti AS text) IS NOT NULL
                          AND consumed.salesbot_token_jti = CAST(:salesbot_token_jti AS text)
                          AND consumed.callback_claims ->> 'iat' = :salesbot_token_iat_text
                     )
              )
              AND (
                  (:entity_type = 'leads' AND candidate.lead_id = :entity_id)
                  OR (:entity_type = 'contacts' AND candidate.contact_id = :entity_id)
              )
              AND (
                  CAST(:widget_contact_id AS text) IS NULL
                  OR candidate.contact_id = CAST(:widget_contact_id AS text)
              )
              AND candidate.interaction_type = CAST(:interaction_type AS text)
              AND (
                  CAST(:expected_channel AS text) IS NULL
                  OR candidate.channel = CAST(:expected_channel AS text)
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

    if values["interaction_type"] == "instagram_comment":
        duplicate = await _find_job_for_callback_identity(values)
        if duplicate:
            logger.info("Kommo Salesbot comment callback ignored as duplicate for job: %s", _job_log_context(dict(duplicate)))
            return {"status": "duplicate", "job_id": str(duplicate["id"])}
        return await _create_ready_comment_job_from_callback(data, values)

    if values["expected_channel"] and await _has_waiting_job_for_other_channel(values):
        logger.warning("Kommo Salesbot callback ignored: reason=expected_channel_mismatch")
        return {"status": "ignored", "reason": "expected_channel_mismatch"}

    duplicate = await _find_job_for_callback_identity(values)
    if duplicate and duplicate["status"] in _TERMINAL_STATUSES | _ACTIVE_SALESBOT_STATUSES:
        logger.info("Kommo Salesbot callback ignored as duplicate for job: %s", _job_log_context(dict(duplicate)))
        return {"status": "duplicate", "job_id": str(duplicate["id"])}
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
            processing_lease_id = NULL,
            completed_at = NOW()
        WHERE status = 'waiting_for_salesbot'
          AND COALESCE(salesbot_launched_at, updated_at, created_at) < NOW() - (:minutes * INTERVAL '1 minute')
        """,
        {"minutes": STALE_WAITING_MINUTES},
    )
    reset_processing = await db.execute(
        """
        UPDATE kommo_message_jobs
        SET status = CASE
                WHEN channel = 'instagram'
                     AND interaction_type = 'private_message'
                     AND talk_id IS NOT NULL
                THEN 'ready'
                WHEN return_url IS NULL THEN 'pending'
                ELSE 'ready'
            END,
            processing_started_at = NULL,
            processing_lease_id = NULL,
            updated_at = NOW(),
            last_error = 'Recovered stale processing job'
        WHERE status = 'processing'
          AND processing_started_at < NOW() - (:minutes * INTERVAL '1 minute')
          AND ai_started_at IS NULL
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
            processing_lease_id = NULL,
            last_error = CASE
                WHEN status = 'continuing' THEN 'Continuation outcome unknown after stale timeout'
                ELSE 'AI execution outcome unknown; manual reconciliation required'
            END
        WHERE (status = 'continuing' OR (status = 'processing' AND ai_started_at IS NOT NULL))
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
            processing_started_at = NULL,
            processing_lease_id = NULL,
            last_error = 'Max attempts exceeded during stale recovery'
        WHERE status = 'processing'
          AND processing_started_at < NOW() - (:minutes * INTERVAL '1 minute')
          AND ai_started_at IS NULL
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
    interaction_rows = await db.fetch_all(
        """
        SELECT COALESCE(interaction_type, 'private_message') AS interaction_type, COUNT(*) AS cnt
        FROM kommo_message_jobs
        GROUP BY COALESCE(interaction_type, 'private_message')
        """
    )
    interaction_counts = {row["interaction_type"]: row["cnt"] for row in interaction_rows}
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
        "pending_job_count": counts.get("pending", 0) + counts.get("prepared", 0) + counts.get("waiting_for_salesbot", 0) + counts.get("waiting_for_context", 0) + counts.get("waiting_for_delivery", 0) + counts.get("ready", 0) + counts.get("processing", 0) + counts.get("continuing", 0),
        "failed_job_count": counts.get("failed", 0) + counts.get("delivery_unknown", 0),
        "stale_job_count": stale["cnt"] if stale else 0,
        "last_successful_incoming_webhook_at": timestamps["last_incoming"] if timestamps else None,
        "last_successful_salesbot_launch_at": timestamps["last_launch"] if timestamps else None,
        "last_successful_widget_callback_at": timestamps["last_callback"] if timestamps else None,
        "last_successful_continuation_at": timestamps["last_continuation"] if timestamps else None,
        "last_kommo_api_error_summary": sanitize_job_error(timestamps["last_error"]) if timestamps and timestamps["last_error"] else None,
        "job_counts_by_interaction_type": interaction_counts,
    }


async def _claim_due_pending_job():
    return await db.fetch_one(
        """
        UPDATE kommo_message_jobs job
        SET status = 'processing',
            processing_started_at = NOW(),
            processing_lease_id = gen_random_uuid(),
            ai_started_at = NULL,
            attempt_count = attempt_count + 1,
            updated_at = NOW()
        WHERE job.id = (
            SELECT candidate.id
            FROM kommo_message_jobs candidate
            WHERE candidate.status = 'pending'
              AND candidate.buffer_expires_at <= NOW()
              AND pg_try_advisory_xact_lock(
                    hashtext('kommo-salesbot:' || COALESCE(candidate.lead_id, candidate.contact_id, candidate.correlation_id))
              )
               AND NOT EXISTS (
                   SELECT 1 FROM kommo_message_jobs active
                   WHERE active.status IN ('prepared', 'waiting_for_salesbot', 'waiting_for_context', 'waiting_for_delivery', 'ready', 'processing', 'continuing')
                    AND active.id <> candidate.id
                    AND (
                        (candidate.lead_id IS NOT NULL AND active.lead_id = candidate.lead_id)
                        OR (candidate.contact_id IS NOT NULL AND active.contact_id = candidate.contact_id)
                        OR (
                            candidate.lead_id IS NULL
                            AND candidate.contact_id IS NULL
                            AND active.correlation_id = candidate.correlation_id
                        )
                    )
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
        SELECT id, correlation_id, status, channel, interaction_type, origin, lead_id, contact_id, chat_id, talk_id,
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
        SELECT id, status, channel, interaction_type, origin, lead_id, contact_id, chat_id, talk_id,
               combined_message, media_url, return_url, attempt_count, buffer_expires_at,
               salesbot_launched_at, processing_started_at, created_at, updated_at, last_error
        FROM kommo_message_jobs
        WHERE correlation_id = :correlation_id
          AND status IN ('prepared', 'waiting_for_salesbot', 'waiting_for_context', 'waiting_for_delivery', 'ready', 'processing', 'continuing')
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
            processing_lease_id = gen_random_uuid(),
            ai_started_at = NOW(),
            attempt_count = attempt_count + 1,
            updated_at = NOW()
        WHERE job.id = (
            SELECT candidate.id
            FROM kommo_message_jobs candidate
            WHERE candidate.status = 'ready'
              AND (
                  candidate.return_url IS NOT NULL
                  OR (
                      candidate.channel = 'instagram'
                      AND candidate.interaction_type = 'private_message'
                      AND candidate.talk_id IS NOT NULL
                  )
              )
            ORDER BY candidate.updated_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING *
        """
    )


async def _prepare_direct_instagram_dm(job: dict) -> None:
    processing_lease_id = job.get("processing_lease_id")
    if not _is_valid_kommo_id(job.get("talk_id")):
        logger.warning(
            "Kommo direct Instagram job failed before preparation: job_id=%s reason=missing_instagram_talk_id",
            job["id"],
        )
        await _mark_job(
            job["id"],
            "failed",
            "missing_instagram_talk_id",
            processing_lease_id=processing_lease_id,
        )
        return

    config = get_config()
    try:
        if await _discard_private_job_if_superseded_by_recent_comment(job):
            return
        logger.info("Kommo direct Instagram preparation started: %s", _job_log_context(job))
        settings = await db.get_settings()
        client = KommoClient.from_config()
        lead = await client.get_lead(job["lead_id"]) if job.get("lead_id") else None
        ai_mode_enum = None
        if lead:
            ai_mode_enum, initialized_now = await ensure_ai_mode_initialized(client, str(job["lead_id"]), lead)
            if initialized_now:
                logger.info("Initialized Kommo AI Mode for lead %s", job["lead_id"])

        profile = build_kommo_customer_profile(job=job, contact=None)
        customer = await resolve_customer_from_kommo_job(job, lead=lead, profile=profile)
        if ai_mode_enum is not None:
            synced_customer = await sync_local_state_from_ai_mode(customer["id"], ai_mode_enum)
            if isinstance(synced_customer, dict):
                customer.update(synced_customer)
        ai_mode_enum = await _reactivate_expired_escalation_if_needed(customer, ai_mode_enum)

        decision = evaluate_automation_state(
            ai_enabled=bool(settings.get("ai_enabled", True)),
            local_conversation_state=customer.get("conversation_state"),
            kommo_ai_mode_enum_id=ai_mode_enum,
            job_status=job.get("status"),
        )
        wait_for_story_context = bool(config.meta_story_context_enabled)
        defer_suppression = not decision.allowed and wait_for_story_context
        if not decision.allowed and not defer_suppression:
            logger.info(
                "Kommo direct Instagram job suppressed before preparation: job_id=%s reason=%s",
                job["id"],
                decision.reason,
            )
            await _store_user_message_if_suppressed(customer, job)
            await _mark_job(
                job["id"],
                "discarded" if not decision.needs_ai_mode_initialization else "failed",
                decision.reason,
                processing_lease_id=processing_lease_id,
            )
            return
        if defer_suppression:
            logger.info(
                "Kommo direct Instagram job suppression deferred until after Story context: job_id=%s reason=%s",
                job["id"],
                decision.reason,
            )

        if await _discard_private_job_if_superseded_by_recent_comment(job):
            return
        prepared_job = await _mark_direct_instagram_job_prepared(
            job["id"],
            processing_lease_id,
            wait_for_story_context=wait_for_story_context,
            story_context_wait_seconds=config.meta_story_context_wait_seconds,
            suppress_after_context=defer_suppression,
            automation_block_reason=decision.reason if defer_suppression else None,
        )
        if not prepared_job:
            logger.info(
                "Kommo direct Instagram preparation skipped because job was no longer processing: job_id=%s",
                job["id"],
            )
            return
        if await _discard_private_job_if_superseded_by_recent_comment(dict(prepared_job)):
            return
        logger.info(
            "Kommo direct Instagram job prepared without Salesbot: %s",
            _job_log_context(dict(prepared_job)),
        )
    except Exception as e:
        logger.exception(
            "Kommo direct Instagram preparation failed: job_id=%s error=%s",
            job["id"],
            sanitize_job_error(e),
        )
        await _mark_job(job["id"], "failed", sanitize_job_error(e), processing_lease_id=processing_lease_id)


async def _mark_direct_instagram_job_prepared(
    job_id: str,
    processing_lease_id: str | None,
    *,
    wait_for_story_context: bool,
    story_context_wait_seconds: int,
    suppress_after_context: bool,
    automation_block_reason: str | None,
):
    return await db.fetch_one(
        """
        UPDATE kommo_message_jobs
        SET status = CASE WHEN :wait_for_story_context THEN 'waiting_for_context' ELSE 'ready' END,
            context_status = CASE WHEN :wait_for_story_context THEN 'pending' ELSE 'not_required' END,
            context_deadline_at = CASE
                WHEN :wait_for_story_context
                THEN NOW() + (:story_context_wait_seconds * INTERVAL '1 second')
                ELSE NULL
            END,
            suppress_after_context = :suppress_after_context,
            automation_block_reason = :automation_block_reason,
            processing_started_at = NULL,
            processing_lease_id = NULL,
            ai_started_at = NULL,
            updated_at = NOW()
        WHERE id = :id
          AND status = 'processing'
          AND processing_lease_id = CAST(:processing_lease_id AS uuid)
        RETURNING *
        """,
        {
            "id": job_id,
            "processing_lease_id": processing_lease_id,
            "wait_for_story_context": wait_for_story_context,
            "story_context_wait_seconds": story_context_wait_seconds,
            "suppress_after_context": suppress_after_context,
            "automation_block_reason": automation_block_reason,
        },
    )


async def _launch_salesbot_for_job(job: dict) -> None:
    processing_lease_id = job.get("processing_lease_id")
    if _job_interaction_type(job) == "instagram_comment":
        logger.info(
            "Kommo Instagram comment job will not launch Salesbot from backend: job_id=%s",
            job["id"],
        )
        await _mark_job(
            job["id"],
            "failed",
            "instagram_comment_requires_native_salesbot_callback",
            processing_lease_id=processing_lease_id,
        )
        return

    try:
        salesbot_id = _salesbot_id_for_channel(job.get("channel"))
        logger.info("Kommo Salesbot launch preparing job: %s", _job_log_context(job))
        settings = await db.get_settings()
        client = KommoClient.from_config()
        lead = await client.get_lead(job["lead_id"]) if job.get("lead_id") else None
        ai_mode_enum = None
        if lead:
            ai_mode_enum, initialized_now = await ensure_ai_mode_initialized(client, str(job["lead_id"]), lead)
            if initialized_now:
                logger.info("Initialized Kommo AI Mode for lead %s", job["lead_id"])

        profile = build_kommo_customer_profile(job=job, contact=None)
        customer = await resolve_customer_from_kommo_job(job, lead=lead, profile=profile)
        if ai_mode_enum is not None:
            synced_customer = await sync_local_state_from_ai_mode(customer["id"], ai_mode_enum)
            if isinstance(synced_customer, dict):
                customer.update(synced_customer)
        ai_mode_enum = await _reactivate_expired_escalation_if_needed(customer, ai_mode_enum)

        decision = evaluate_automation_state(
            ai_enabled=bool(settings.get("ai_enabled", True)),
            local_conversation_state=customer.get("conversation_state"),
            kommo_ai_mode_enum_id=ai_mode_enum,
            job_status=job.get("status"),
        )
        if not decision.allowed:
            logger.info(
                "Kommo WhatsApp job suppressed before Salesbot launch: job_id=%s reason=%s",
                job["id"],
                decision.reason,
            )
            await _store_user_message_if_suppressed(customer, job)
            await _mark_job(
                job["id"],
                "discarded" if not decision.needs_ai_mode_initialization else "failed",
                decision.reason,
                processing_lease_id=processing_lease_id,
            )
            return

        entity_id = job.get("lead_id") or job.get("contact_id")
        entity_type = "leads" if job.get("lead_id") else "contacts"
        if not entity_id:
            logger.warning("Kommo WhatsApp job failed before Salesbot launch: job_id=%s reason=missing_entity_id", job["id"])
            await _mark_job(job["id"], "failed", "missing_entity_id", processing_lease_id=processing_lease_id)
            return
        waiting_job = await _mark_job_waiting_for_salesbot(
            job["id"],
            processing_lease_id,
        )
        if not waiting_job:
            logger.info("Kommo Salesbot launch skipped because job was no longer processing: job_id=%s", job["id"])
            return
        logger.info("Kommo job waiting for Salesbot callback before launch: job_id=%s", job["id"])
        try:
            await client.run_salesbot(entity_id, entity_type, salesbot_id)
        except KommoAPIError as e:
            error = sanitize_job_error(e)
            if _is_definitive_launch_error(e):
                logger.warning("Kommo Salesbot launch rejected definitively: job_id=%s error=%s", job["id"], error)
                await _mark_waiting_job_failed(job["id"], processing_lease_id, error)
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
        await _mark_job(job["id"], "failed", sanitize_job_error(e), processing_lease_id=processing_lease_id)


async def _mark_job_waiting_for_salesbot(
    job_id: str,
    processing_lease_id: str | None,
):
    return await db.fetch_one(
        """
        UPDATE kommo_message_jobs
        SET status = 'waiting_for_salesbot',
            salesbot_launched_at = NOW(),
            processing_started_at = NULL,
            updated_at = NOW()
        WHERE id = :id
          AND status = 'processing'
          AND processing_lease_id = CAST(:processing_lease_id AS uuid)
        RETURNING *
        """,
        {
            "id": job_id,
            "processing_lease_id": processing_lease_id,
        },
    )


def _is_definitive_launch_error(error: KommoAPIError) -> bool:
    return error.status_code is not None and 400 <= error.status_code < 500 and error.status_code != 429


async def _mark_waiting_job_failed(job_id: str, processing_lease_id: str | None, error: str) -> None:
    await db.execute(
        """
        UPDATE kommo_message_jobs
        SET status = 'failed',
            last_error = :last_error,
            processing_started_at = NULL,
            completed_at = NOW(),
            processing_lease_id = NULL,
            updated_at = NOW()
        WHERE id = :id
          AND status = 'waiting_for_salesbot'
          AND processing_lease_id = CAST(:processing_lease_id AS uuid)
        """,
        {
            "id": job_id,
            "processing_lease_id": processing_lease_id,
            "last_error": sanitize_job_error(error),
        },
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


async def _reactivate_expired_escalation_if_needed(customer: dict, ai_mode_enum: int | None) -> int | None:
    result = await escalations.reactivate_if_expired(customer)
    if result.status != "reactivated":
        return ai_mode_enum

    if result.customer:
        customer.update(result.customer)
    customer["conversation_state"] = "active"
    if result.provider == "kommo":
        return get_config().kommo_ai_active_enum_id
    return ai_mode_enum


def _single_contact_lead_id(contact: dict | None) -> str | None:
    embedded = (contact or {}).get("_embedded") or {}
    leads = embedded.get("leads") or []
    lead_ids = []
    for lead in leads:
        if not isinstance(lead, dict):
            continue
        lead_id = lead.get("id")
        if lead_id is not None:
            lead_ids.append(str(lead_id))
    return lead_ids[0] if len(set(lead_ids)) == 1 else None


async def _process_ready_job(job: dict) -> None:
    config = get_config()
    client = KommoClient.from_config()
    continuation_started = False
    media_delivery_succeeded = False
    customer = None
    try:
        logger.info("Kommo ready job processing started: %s", _job_log_context(job))
        settings = await db.get_settings()
        contact = await _fetch_contact_for_job(client, job)
        lead_id = job.get("lead_id") or _single_contact_lead_id(contact)
        lead = await client.get_lead(lead_id) if lead_id else None
        ai_mode_enum = extract_ai_mode_enum_from_lead(lead or {}, config) if lead else None
        if lead and _job_interaction_type(job) == "instagram_comment":
            ai_mode_enum, initialized_now = await ensure_ai_mode_initialized(
                client,
                str(lead_id),
                lead,
            )
            if initialized_now:
                logger.info(
                    "Initialized Kommo AI Mode for Instagram comment lead %s",
                    lead_id,
                )
        profile = build_kommo_customer_profile(job=job, contact=contact)
        customer = await resolve_customer_from_kommo_job(job, lead=lead, contact=contact, profile=profile)
        if _has_unsupported_attachment(job):
            await _continue_and_discard_job(
                client,
                job,
                "unsupported_attachment",
                customer_message=UNSUPPORTED_ATTACHMENT_MESSAGE,
                customer=customer,
            )
            return
        effective_message_text = await _effective_customer_message(job)
        job = {
            **job,
            "combined_message": effective_message_text,
            "media_url": _job_image_media_url(job),
        }
        current_private_context = _job_instagram_content_context(job)
        if (
            job.get("channel") == "instagram"
            and _job_interaction_type(job) == "private_message"
            and current_private_context.get("source") == "story_reply"
            and current_private_context.get("meta_sender_id")
        ):
            identity_result = await persist_verified_meta_instagram_sender(
                customer_id=str(customer["id"]),
                external_author_id=current_private_context["meta_sender_id"],
            )
            logger.info(
                "Meta Instagram sender mapping result: status=%s",
                identity_result.get("status"),
            )
            if identity_result.get("status") == "conflict":
                current_private_context = {}
                await db.execute(
                    """
                    UPDATE kommo_message_jobs
                    SET instagram_content_context = '{}'::jsonb,
                        updated_at = NOW()
                    WHERE id = :id
                    """,
                    {"id": job["id"]},
                )

        incoming_instagram_context = {}
        current_story_context = False
        has_current_story_event = False
        is_instagram_private_message = (
            job.get("channel") == "instagram"
            and _job_interaction_type(job) == "private_message"
        )
        if is_instagram_private_message:
            has_current_story_event = bool(job.get("meta_context_event_id"))
            if _is_resolved_story_context(current_private_context):
                current_story_context = True
                incoming_instagram_context = await sessions.store_instagram_content_context(
                    str(customer["id"]),
                    current_private_context,
                    ttl_hours=config.instagram_story_context_ttl_hours,
                )
            elif has_current_story_event:
                await sessions.clear_instagram_content_context(str(customer["id"]))

        if job.get("suppress_after_context"):
            suppression_reason = job.get("automation_block_reason") or "automation_suppressed_before_context"
            logger.info(
                "Kommo ready job honoring deferred suppression: job_id=%s reason=%s",
                job["id"],
                suppression_reason,
            )
            await _store_user_message_if_suppressed(customer, job)
            await _continue_and_discard_job(client, job, suppression_reason)
            return

        if ai_mode_enum is not None:
            synced_customer = await sync_local_state_from_ai_mode(customer["id"], ai_mode_enum)
            if isinstance(synced_customer, dict):
                customer.update(synced_customer)
        ai_mode_enum = await _reactivate_expired_escalation_if_needed(customer, ai_mode_enum)

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

        if (
            is_instagram_private_message
            and not current_story_context
            and not has_current_story_event
        ):
            incoming_instagram_context = await sessions.load_active_instagram_content_context(
                str(customer["id"])
            )
            if incoming_instagram_context:
                await db.execute(
                    """
                    UPDATE kommo_message_jobs
                    SET instagram_content_context = CAST(:context AS jsonb),
                        updated_at = NOW()
                    WHERE id = :id AND status = 'processing'
                    """,
                    {
                        "id": job["id"],
                        "context": json.dumps(
                            incoming_instagram_context | {"context_usage": "reused"},
                            ensure_ascii=False,
                        ),
                    },
                )

        sender_id = _local_sender_id(job)
        media_url = job.get("media_url")
        result = await generate_response(
            channel=job.get("channel") or "whatsapp",
            sender_id=sender_id,
            message_text=job["combined_message"],
            media_url=media_url,
            customer_profile=profile.as_customer_profile(),
            customer_id=str(customer["id"]),
            integration_context={
                "provider": "kommo",
                "lead_id": lead_id,
                "contact_id": job.get("contact_id"),
                "chat_id": job.get("chat_id"),
                "talk_id": job.get("talk_id"),
                "author_id": job.get("author_id"),
                "interaction_type": _job_interaction_type(job),
                "media_url_is_direct": bool(media_url),
                "public_comment_context": _job_public_comment_context(job),
                "incoming_instagram_context": incoming_instagram_context,
                "current_story_context": current_story_context,
            },
            persist_assistant_message=False,
            message_source_id=_conversation_source_id(job),
        )
        if result.get("customer_id"):
            await upsert_mapping(
                customer_id=result["customer_id"],
                provider="kommo",
                channel=job.get("channel") or "whatsapp",
                external_contact_id=job.get("contact_id"),
                external_lead_id=lead_id,
                external_chat_id=job.get("chat_id"),
                external_talk_id=job.get("talk_id"),
                external_author_id=job.get("author_id"),
                external_origin=job.get("origin"),
            )

        if not result.get("escalated"):
            lead_after = await client.get_lead(lead_id) if lead_id else None
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

        enabled_media_types = get_enabled_media_types(job, result, config=config)
        mapped = map_ai_response_to_salesbot(
            result,
            native_media=bool(enabled_media_types),
            native_media_types=enabled_media_types,
        )
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
            interaction_type=_job_interaction_type(job),
        )
        if not customer_text:
            logger.info(
                "Kommo ready job produced no deliverable reply after transport sanitization: job_id=%s reason=empty_after_sanitization",
                job["id"],
            )
            await _continue_and_discard_job(client, job, "empty_after_sanitization")
            return
        delivery_result = await deliver_response(
            job=job,
            result=result,
            customer_text=customer_text,
            client=client,
        )
        if _is_direct_instagram_dm(job):
            media_delivery_succeeded = True
            if not await _mark_direct_job_waiting_for_delivery(
                job["id"],
                job.get("processing_lease_id"),
                str(job.get("talk_id") or ""),
                delivery_result.provider_message_ids,
                _pending_assistant_payload(
                    customer,
                    result,
                    delivery_result.customer_text,
                    delivery_result.delivered_attachments,
                    final_status="sent",
                ),
            ):
                raise KommoDeliveryStateError(
                    "Kommo direct Instagram job could not enter delivery reconciliation"
                )
            return
        if delivery_result.transport == "salesbot":
            fallback_mapped = map_ai_response_to_salesbot(result)
            fallback_text = (fallback_mapped.customer_text or "").strip()
            if not fallback_text:
                await _continue_and_discard_job(
                    client,
                    job,
                    fallback_mapped.reason or "empty_response",
                )
                return
            customer_text, message_diagnostics = prepare_kommo_customer_message(
                fallback_text,
                job.get("channel") or "whatsapp",
                settings,
                interaction_type=_job_interaction_type(job),
            )
            continuation_data = {
                "status": "success",
                "delivery_mode": "salesbot",
                "message": customer_text,
            }
        else:
            media_delivery_succeeded = True
            customer_text = delivery_result.customer_text
            continuation_data = {
                "status": "success",
                "delivery_mode": "chats_api",
                "message": "",
            }
            await _store_assistant_message_after_delivery(
                customer,
                job,
                result,
                delivery_result.customer_text,
                delivered_attachments=delivery_result.delivered_attachments,
                expected_job_status=None,
            )
        if not await _mark_job_continuing(job["id"], job.get("processing_lease_id"), continuation_data):
            logger.warning("Kommo ready job lost its processing lease before continuation: job_id=%s", job["id"])
            return
        continuation_started = True
        _log_continuation_prepared(job["id"], continuation_data, message_diagnostics)
        try:
            response_payload = await client.continue_salesbot(
                job["return_url"],
                data=continuation_data,
            )
        except KommoAPIError as e:
            logger.warning(
                "Kommo Salesbot continuation failed: job_id=%s interaction_type=%s error=%s",
                job["id"],
                _job_interaction_type(job),
                sanitize_job_error(e),
            )
            raise
        except Exception as e:
            logger.warning(
                "Kommo Salesbot continuation failed: job_id=%s interaction_type=%s error=%s",
                job["id"],
                _job_interaction_type(job),
                sanitize_job_error(e),
            )
            raise
        logger.info(
            "Kommo Salesbot continuation succeeded: job_id=%s interaction_type=%s",
            job["id"],
            _job_interaction_type(job),
        )
        if not media_delivery_succeeded:
            await _store_assistant_message_after_delivery(
                customer,
                job,
                result,
                customer_text,
                delivered_attachments=None,
            )
        await _mark_job_sent(job["id"], job.get("processing_lease_id"), response_payload)
    except AudioTranscriptionError as e:
        error = sanitize_job_error(e)
        logger.warning("Kommo voice transcription failed: job_id=%s error=%s", job["id"], error)
        if e.retryable and int(job.get("attempt_count") or 0) < MAX_JOB_ATTEMPTS:
            await _retry_ready_job(job, error)
        else:
            await _fail_ready_job(client, job, error, customer=customer)
    except KommoDeliveryAbortedError as e:
        logger.info(
            "Kommo direct Instagram delivery aborted before network send: job_id=%s reason=%s",
            job["id"],
            sanitize_job_error(e),
        )
        await _mark_job(
            job["id"],
            "failed",
            sanitize_job_error(e),
            processing_lease_id=job.get("processing_lease_id"),
        )
    except KommoPartialDeliveryError as e:
        logger.warning(
            "Kommo media response partially delivered: job_id=%s delivered_count=%s",
            job["id"],
            len(e.delivered_attachments),
        )
        try:
            await _store_assistant_message_after_delivery(
                customer,
                job,
                result,
                e.customer_text,
                delivered_attachments=e.delivered_attachments,
                expected_job_status=None,
            )
        except Exception as persistence_error:
            logger.exception(
                "Kommo partial media history persistence failed: job_id=%s error=%s",
                job["id"],
                sanitize_job_error(persistence_error),
            )
        await _mark_job(
            job["id"],
            "delivery_unknown",
            sanitize_job_error(e),
            processing_lease_id=job.get("processing_lease_id"),
        )
    except KommoDeliveryUnknownError as e:
        logger.warning(
            "Kommo media delivery requires manual reconciliation: job_id=%s error=%s",
            job["id"],
            sanitize_job_error(e),
        )
        await _mark_job(
            job["id"],
            "delivery_unknown",
            sanitize_job_error(e),
            processing_lease_id=job.get("processing_lease_id"),
        )
    except KommoDeliveryStateError as e:
        if await _send_direct_failure_fallback_if_safe(client, job, customer, e):
            return
        logger.warning(
            "Kommo media delivery requires manual reconciliation: job_id=%s error=%s",
            job["id"],
            sanitize_job_error(e),
        )
        await _mark_job(
            job["id"],
            "delivery_unknown",
            sanitize_job_error(e),
            processing_lease_id=job.get("processing_lease_id"),
        )
    except KommoAPIError as e:
        logger.warning("Kommo ready job API error: job_id=%s error=%s", job["id"], sanitize_job_error(e))
        if (
            not continuation_started
            and not media_delivery_succeeded
            and job.get("return_url")
            and not _is_direct_instagram_dm(job)
        ):
            await _continue_and_discard_job(
                client,
                job,
                sanitize_job_error(e),
                send_customer_fallback=True,
                customer=customer,
            )
            return
        if (
            not continuation_started
            and not media_delivery_succeeded
            and await _send_direct_failure_fallback_if_safe(client, job, customer, e)
        ):
            return
        await _mark_job(
            job["id"],
            (
                "delivery_unknown"
                if media_delivery_succeeded
                else _status_after_continuation_error(e, continuation_started)
            ),
            sanitize_job_error(e),
            processing_lease_id=job.get("processing_lease_id"),
        )
    except Exception as e:
        logger.exception("Kommo ready job failed: job_id=%s error=%s", job["id"], sanitize_job_error(e))
        if (
            not continuation_started
            and not media_delivery_succeeded
            and job.get("return_url")
            and not _is_direct_instagram_dm(job)
        ):
            await _continue_and_discard_job(
                client,
                job,
                sanitize_job_error(e),
                send_customer_fallback=True,
                customer=customer,
            )
            return
        if (
            not continuation_started
            and not media_delivery_succeeded
            and await _send_direct_failure_fallback_if_safe(client, job, customer, e)
        ):
            return
        await _mark_job(
            job["id"],
            "delivery_unknown"
            if continuation_started or media_delivery_succeeded
            else "failed",
            sanitize_job_error(e),
            processing_lease_id=job.get("processing_lease_id"),
        )


async def _direct_customer_send_begun(job_id: str) -> bool:
    row = await db.fetch_one(
        """
        SELECT EXISTS (
            SELECT 1
            FROM kommo_outbound_deliveries
            WHERE job_id = CAST(:job_id AS uuid)
              AND transport = 'chats_api'
              AND (
                  status IN ('sending', 'accepted', 'confirmed', 'delivery_unknown')
                  OR (
                      attachment_metadata->>'send_attempt_count' ~ '^[0-9]+$'
                      AND (attachment_metadata->>'send_attempt_count')::integer >= 1
                  )
              )
        ) AS send_begun
        """,
        {"job_id": job_id},
    )
    return bool(row and row["send_begun"])


async def _send_direct_failure_fallback_if_safe(
    client: KommoClient,
    job: dict,
    customer: dict | None,
    error: Exception,
) -> bool:
    if not _is_direct_instagram_dm(job) or customer is None:
        return False
    try:
        send_begun = await _direct_customer_send_begun(str(job["id"]))
    except Exception as lookup_error:
        logger.warning(
            "Kommo direct fallback safety check failed closed: job_id=%s error=%s",
            job.get("id"),
            sanitize_job_error(lookup_error),
        )
        return False
    if send_begun:
        return False
    await _continue_and_discard_job(
        client,
        job,
        sanitize_job_error(error),
        send_customer_fallback=True,
        customer=customer,
    )
    return True


async def _fetch_contact_for_job(client: KommoClient, job: dict) -> dict | None:
    contact_id = job.get("contact_id")
    if not contact_id:
        return None
    try:
        return await client.get_contact(contact_id)
    except KommoAPIError as e:
        logger.warning(
            "Kommo contact enrichment skipped: job_id=%s contact_id=%s error=%s",
            job.get("id"),
            contact_id,
            sanitize_job_error(e),
        )
    except Exception as e:
        logger.warning(
            "Kommo contact enrichment skipped: job_id=%s contact_id=%s error=%s",
            job.get("id"),
            contact_id,
            sanitize_job_error(e),
        )
    return None


async def _find_job_for_callback_identity(values: dict):
    query_values = {
        "return_url": values["return_url"],
        "salesbot_token_jti": values["salesbot_token_jti"],
        "salesbot_token_iat_text": values["salesbot_token_iat_text"],
    }
    query = """
        SELECT * FROM kommo_message_jobs
        WHERE (
               return_url = :return_url
               AND callback_claims ->> 'iat' = :salesbot_token_iat_text
              )
           OR (
                CAST(:salesbot_token_jti AS text) IS NOT NULL
                AND salesbot_token_jti = CAST(:salesbot_token_jti AS text)
                AND callback_claims ->> 'iat' = :salesbot_token_iat_text
           )
        ORDER BY updated_at DESC
        LIMIT 1
    """
    return await db.fetch_one(query, query_values)


async def _has_waiting_job_for_other_channel(values: dict) -> bool:
    job = await db.fetch_one(
        """
        SELECT id
        FROM kommo_message_jobs candidate
        WHERE candidate.status = 'waiting_for_salesbot'
          AND candidate.interaction_type = 'private_message'
          AND candidate.channel <> CAST(:expected_channel AS text)
          AND (
              (:entity_type = 'leads' AND candidate.lead_id = :entity_id)
              OR (:entity_type = 'contacts' AND candidate.contact_id = :entity_id)
          )
          AND (
              CAST(:widget_contact_id AS text) IS NULL
              OR candidate.contact_id = CAST(:widget_contact_id AS text)
          )
        LIMIT 1
        """,
        {
            "expected_channel": values["expected_channel"],
            "entity_type": values["entity_type"],
            "entity_id": values["entity_id"],
            "widget_contact_id": values["widget_contact_id"],
        },
    )
    return job is not None


async def _create_ready_comment_job_from_callback(data: SalesbotWidgetData, values: dict) -> dict:
    message = _callback_comment_text(data)
    public_comment_context = _public_comment_context_from_callback(data)
    logger.info(
        "Kommo native comment callback ready job creation started: interaction_type=instagram_comment "
        "message_text_resolved=%s context_keys=%s signed_entity_type=%s signed_entity_id=%s",
        True,
        sorted(public_comment_context.keys()),
        values["entity_type"],
        values["entity_id"],
    )
    external_message_id, correlation_id = _comment_callback_ids(values, message)
    normalized_message = _normalize_reconciliation_message(message)
    message_hash = _message_hash(normalized_message)
    config = get_config()
    context_enabled = bool(getattr(config, "meta_instagram_context_enabled", False))
    initial_status = "waiting_for_context" if context_enabled else "ready"
    context_status = "pending" if context_enabled else "not_required"
    job_values = {
        "correlation_id": correlation_id,
        "external_message_id": external_message_id,
        "lead_id": values["entity_id"] if values["entity_type"] == "leads" else None,
        "contact_id": values["entity_id"] if values["entity_type"] == "contacts" else None,
        "origin": _instagram_callback_origin(data.origin),
        "channel": "instagram",
        "interaction_type": "instagram_comment",
        "combined_message": message,
        "return_url": values["return_url"],
        "callback_claims": values["callback_claims"],
        "public_comment_context": json.dumps(public_comment_context, ensure_ascii=False) if public_comment_context else None,
        "salesbot_token_jti": values["salesbot_token_jti"],
        "salesbot_account_id": values["salesbot_account_id"],
        "salesbot_user_id": values["salesbot_user_id"],
        "salesbot_client_uuid": values["salesbot_client_uuid"],
        "author_username": values["author_username"],
        "author_profile_url": values["author_profile_url"],
        "sender_username": values["sender_username"],
        "sender_profile_url": values["sender_profile_url"],
        "initial_status": initial_status,
        "context_status": context_status,
        "context_wait_seconds": getattr(config, "meta_context_wait_seconds", 10),
    }

    async with db.get_db().transaction():
        await db.execute(
            """
            SELECT pg_advisory_xact_lock(hashtext(:dedupe_key)),
                   pg_advisory_xact_lock(hashtext(:reconciliation_key))
            """,
            {
                "dedupe_key": external_message_id,
                "reconciliation_key": _comment_reconciliation_key(values, message_hash),
            },
        )
        await _discard_recent_private_jobs_superseded_by_comment_callback(
            values=values,
            normalized_message=normalized_message,
            comment_event_timestamp=values["salesbot_token_iat"],
        )
        duplicate = await _find_duplicate_comment_callback_job(
            external_message_id=external_message_id,
            salesbot_token_jti=values["salesbot_token_jti"],
            salesbot_token_iat_text=values["salesbot_token_iat_text"],
            entity_type=values["entity_type"],
            entity_id=values["entity_id"],
            return_url=values["return_url"],
            normalized_message=normalized_message,
        )
        if duplicate:
            logger.info("Kommo native comment callback ignored as duplicate for job: %s", _job_log_context(dict(duplicate)))
            return {"status": "duplicate", "job_id": str(duplicate["id"])}

        job = await db.fetch_one(
            """
            INSERT INTO kommo_message_jobs (
                correlation_id, external_message_id, lead_id, contact_id, origin, channel,
                author_username, author_profile_url, sender_username, sender_profile_url,
                interaction_type, combined_message, return_url, status, buffer_expires_at,
                context_status, context_deadline_at,
                callback_claims, public_comment_context, salesbot_token_jti, salesbot_account_id,
                salesbot_user_id, salesbot_client_uuid
            ) VALUES (
                :correlation_id, :external_message_id, :lead_id, :contact_id, :origin, :channel,
                :author_username, :author_profile_url, :sender_username, :sender_profile_url,
                :interaction_type, :combined_message, :return_url, :initial_status, NOW(),
                :context_status,
                CASE WHEN :context_status = 'pending'
                    THEN NOW() + (:context_wait_seconds * INTERVAL '1 second')
                    ELSE NULL
                END,
                CAST(:callback_claims AS jsonb), CAST(:public_comment_context AS jsonb), :salesbot_token_jti, :salesbot_account_id,
                :salesbot_user_id, :salesbot_client_uuid
            )
            RETURNING *
            """,
            job_values,
        )
        if not job:
            raise RuntimeError("comment_callback_job_insert_failed")
        await _record_callback_receipt(job_values, str(job["id"]))

    logger.info(
        "Kommo native comment callback created comment job: %s",
        _job_log_context(dict(job)),
    )
    return {"status": initial_status, "job_id": str(job["id"])}


async def _discard_recent_private_jobs_superseded_by_comment_callback(
    *,
    values: dict,
    normalized_message: str,
    comment_event_timestamp: float,
) -> int:
    if not normalized_message:
        return 0
    window_seconds = _comment_mirror_window_seconds()
    normalized_hash = normalized_text_hash(normalized_message)
    rows = await db.fetch_all(
        f"""
        SELECT private_job.id,
               ABS(EXTRACT(EPOCH FROM (
                   COALESCE(receipt.received_at, receipt.created_at, private_job.created_at)
                   - COALESCE(meta_event.event_timestamp, to_timestamp(:comment_event_timestamp))
               ))) AS timestamp_delta_seconds,
               CASE
                   WHEN receipt.received_at IS NOT NULL THEN 'receipt_received_at'
                   WHEN receipt.created_at IS NOT NULL THEN 'receipt_created_at'
                   ELSE 'job_created_at'
               END AS private_timestamp_source,
               CASE
                   WHEN meta_event.event_timestamp IS NOT NULL THEN 'meta_event_timestamp'
                   ELSE 'salesbot_iat'
               END AS comment_timestamp_source
        FROM kommo_message_jobs private_job
        LEFT JOIN LATERAL (
            SELECT event.event_timestamp
            FROM meta_instagram_context_events event
            WHERE event.event_type = 'comment'
              AND event.matched_kommo_job_id = private_job.id
              AND event.normalized_text_hash = :normalized_text_hash
            ORDER BY event.event_timestamp ASC, event.id ASC
            LIMIT 1
        ) meta_event ON TRUE
        LEFT JOIN LATERAL (
            SELECT receipt_row.received_at, receipt_row.created_at
            FROM kommo_message_receipts receipt_row
            WHERE receipt_row.job_id = private_job.id
              AND receipt_row.channel = 'instagram'
              AND receipt_row.interaction_type = 'private_message'
              AND receipt_row.normalized_text_hash = :normalized_text_hash
            ORDER BY ABS(EXTRACT(EPOCH FROM (
                         COALESCE(receipt_row.received_at, receipt_row.created_at)
                         - COALESCE(meta_event.event_timestamp, to_timestamp(:comment_event_timestamp))
                     ))) ASC,
                     receipt_row.created_at ASC,
                     receipt_row.id ASC
            LIMIT 1
        ) receipt ON TRUE
        WHERE private_job.interaction_type = 'private_message'
          AND private_job.channel = 'instagram'
          AND private_job.status IN (
              'pending', 'prepared', 'processing', 'waiting_for_salesbot',
              'waiting_for_context', 'ready'
          )
          AND private_job.continuation_payload IS NULL
          AND private_job.assistant_message_persisted_at IS NULL
          AND {_signed_entity_match_sql('private_job')}
          AND {_normalized_message_sql('private_job.combined_message')} = :normalized_message
          AND ABS(EXTRACT(EPOCH FROM (
              COALESCE(receipt.received_at, receipt.created_at, private_job.created_at)
              - COALESCE(meta_event.event_timestamp, to_timestamp(:comment_event_timestamp))
          ))) <= :window_seconds
        ORDER BY timestamp_delta_seconds ASC, private_job.created_at ASC, private_job.id ASC
        FOR UPDATE OF private_job SKIP LOCKED
        """,
        {
            "entity_type": values["entity_type"],
            "entity_id": values["entity_id"],
            "normalized_message": normalized_message,
            "normalized_text_hash": normalized_hash,
            "comment_event_timestamp": comment_event_timestamp,
            "window_seconds": window_seconds,
        },
    )
    candidate = _unique_best_mirror_candidate(rows)
    if not candidate:
        if rows:
            logger.info(
                "Kommo private Instagram mirror reconciliation left ambiguous candidates untouched: count=%s entity_type=%s entity_id=%s",
                len(rows),
                values["entity_type"],
                values["entity_id"],
            )
        return 0
    discarded = await db.fetch_one(
        """
        UPDATE kommo_message_jobs
        SET status = 'discarded',
            last_error = :reason,
            processing_started_at = NULL,
            processing_lease_id = NULL,
            completed_at = NOW(),
            updated_at = NOW()
        WHERE id = CAST(:job_id AS uuid)
          AND status IN (
              'pending', 'prepared', 'processing', 'waiting_for_salesbot',
              'waiting_for_context', 'ready'
          )
          AND continuation_payload IS NULL
          AND assistant_message_persisted_at IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM kommo_outbound_deliveries outbound
              WHERE outbound.job_id = kommo_message_jobs.id
                AND outbound.transport = 'chats_api'
                AND outbound.status IN ('sending', 'accepted', 'confirmed', 'delivery_unknown')
          )
        RETURNING id
        """,
        {"job_id": str(candidate["id"]), "reason": COMMENT_PRIVATE_SUPERSEDED_REASON},
    )
    if discarded:
        logger.info(
            "Kommo private Instagram mirror discarded after native comment callback: job_id=%s entity_type=%s entity_id=%s timestamp_delta_seconds=%s reason=%s",
            candidate["id"],
            values["entity_type"],
            values["entity_id"],
            candidate["timestamp_delta_seconds"],
            COMMENT_PRIVATE_SUPERSEDED_REASON,
        )
        return 1
    return 0


def _unique_best_mirror_candidate(rows) -> dict | None:
    candidates = [dict(row) for row in rows or []]
    if not candidates:
        return None
    candidates.sort(
        key=lambda candidate: (
            float(candidate["timestamp_delta_seconds"]),
            str(candidate["id"]),
        )
    )
    best = candidates[0]
    best_delta = float(best["timestamp_delta_seconds"])
    if best.get("comment_timestamp_source") == "meta_event_timestamp":
        return best
    if len(candidates) > 1:
        next_delta = float(candidates[1]["timestamp_delta_seconds"])
        if next_delta - best_delta <= COMMENT_MIRROR_AMBIGUITY_SECONDS:
            return None
    if (
        best.get("comment_timestamp_source") != "meta_event_timestamp"
        and best_delta > COMMENT_MIRROR_FALLBACK_MAX_DELTA_SECONDS
    ):
        return None
    if (
        best.get("private_timestamp_source") == "job_created_at"
        and best_delta > COMMENT_MIRROR_FALLBACK_MAX_DELTA_SECONDS
    ):
        return None
    return best


async def _find_duplicate_comment_callback_job(
    *,
    external_message_id: str,
    salesbot_token_jti: str | None,
    salesbot_token_iat_text: str,
    entity_type: str,
    entity_id: str,
    return_url: str,
    normalized_message: str,
):
    return await db.fetch_one(
        f"""
        SELECT *
        FROM kommo_message_jobs
        WHERE interaction_type = 'instagram_comment'
          AND (
              external_message_id = :external_message_id
              OR (
                  CAST(:salesbot_token_jti AS text) IS NOT NULL
                  AND salesbot_token_jti = CAST(:salesbot_token_jti AS text)
                  AND (
                       callback_claims ->> 'iat' = :salesbot_token_iat_text
                  )
              )
              OR (
                  created_at >= NOW() - (:dedup_seconds * INTERVAL '1 second')
                  AND {_signed_entity_match_sql()}
                  AND {_normalized_message_sql('combined_message')} = :normalized_message
                  AND (
                      return_url = :return_url
                      AND callback_claims ->> 'iat' = :salesbot_token_iat_text
                  )
              )
          )
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {
            "external_message_id": external_message_id,
            "salesbot_token_jti": salesbot_token_jti,
            "salesbot_token_iat_text": salesbot_token_iat_text,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "return_url": return_url,
            "normalized_message": normalized_message,
            "dedup_seconds": COMMENT_CALLBACK_DEDUP_SECONDS,
        },
    )


async def _record_callback_receipt(values: dict, job_id: str) -> None:
    await db.execute(
        """
        INSERT INTO kommo_message_receipts (
            external_message_id, job_id, correlation_id, lead_id, contact_id, origin,
            channel, interaction_type, receipt_status, message_text, normalized_text_hash
        ) VALUES (
            :external_message_id, :job_id, :correlation_id, :lead_id, :contact_id, :origin,
            :channel, :interaction_type, 'created', :message_text, :normalized_text_hash
        )
        ON CONFLICT (external_message_id) DO NOTHING
        """,
        {
            "external_message_id": values["external_message_id"],
            "job_id": job_id,
            "correlation_id": values["correlation_id"],
            "lead_id": values["lead_id"],
            "contact_id": values["contact_id"],
            "origin": values["origin"],
            "channel": values["channel"],
            "interaction_type": values["interaction_type"],
            "message_text": values["combined_message"],
            "normalized_text_hash": normalized_text_hash(values["combined_message"]),
        },
    )


def _callback_values(data: SalesbotWidgetData, return_url: str, claims: dict) -> dict:
    entity_type = claims.get("entity_type")
    entity_id = _claim_as_str(claims, "entity_id")
    if entity_type not in {"leads", "contacts"} or not entity_id:
        raise ValueError("missing_signed_entity_identity")
    if entity_type == "leads" and data.lead_id and data.lead_id != entity_id:
        raise ValueError("widget_lead_id_mismatch")
    if entity_type == "contacts" and data.contact_id and data.contact_id != entity_id:
        raise ValueError("widget_contact_id_mismatch")
    try:
        token_iat = float(claims.get("iat"))
    except (TypeError, ValueError) as e:
        raise ValueError("missing_signed_issued_at") from e
    if token_iat <= 0:
        raise ValueError("invalid_signed_issued_at")
    interaction_type = data.interaction_type or "private_message"
    if _has_public_comment_context(data) and interaction_type != "instagram_comment":
        raise ValueError("comment_callback_interaction_type_mismatch")
    if interaction_type == "instagram_comment" and data.expected_channel not in {None, "instagram"}:
        raise ValueError("comment_callback_expected_channel_mismatch")
    return {
        "return_url": return_url,
        "entity_id": entity_id,
        "entity_type": entity_type,
        "widget_contact_id": _clean_widget_id(data.contact_id),
        "callback_claims": json.dumps(_safe_claims(claims)),
        "salesbot_token_jti": _claim_as_str(claims, "jti"),
        "salesbot_token_iat": token_iat,
        "salesbot_token_iat_text": str(claims.get("iat")),
        "salesbot_account_id": _claim_as_str(claims, "account_id"),
        "salesbot_user_id": _claim_as_str(claims, "user_id"),
        "salesbot_client_uuid": _claim_as_str(claims, "client_uid") or _claim_as_str(claims, "client_uuid"),
        "interaction_type": interaction_type,
        "expected_channel": data.expected_channel,
        "author_username": data.author_username,
        "author_profile_url": data.author_profile_url,
        "sender_username": data.sender_username,
        "sender_profile_url": data.sender_profile_url,
    }


def _callback_comment_text(data: SalesbotWidgetData) -> str:
    text = str(data.message or "").strip()
    if not text or (text.startswith("{{") and text.endswith("}}")):
        raise ValueError("missing_comment_message")
    return text


def _public_comment_context_from_callback(data: SalesbotWidgetData) -> dict:
    raw = data.model_dump(exclude_none=True)
    return {
        key: cleaned
        for key, value in raw.items()
        if key in _PUBLIC_COMMENT_CONTEXT_FIELDS
        if (cleaned := _clean_public_comment_context_value(value))
    }


def _clean_public_comment_context_value(value) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text or (text.startswith("{{") and text.endswith("}}")):
        return ""
    return text[:1000]


def _clean_widget_text(value) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text or (text.startswith("{{") and text.endswith("}}")):
        return ""
    return text


def _clean_widget_id(value) -> str | None:
    text = _clean_widget_text(value)
    return text or None


def _has_public_comment_context(data: SalesbotWidgetData) -> bool:
    raw = data.model_dump(exclude_none=True)
    return any(
        key in _PUBLIC_COMMENT_CONTEXT_FIELDS and _clean_public_comment_context_value(value)
        for key, value in raw.items()
    )


def _comment_callback_ids(values: dict, message: str) -> tuple[str, str]:
    normalized_message = _normalize_reconciliation_message(message)
    stable = "|".join(
        str(part or "")
        for part in (
            values.get("salesbot_account_id"),
            values.get("entity_type"),
            values.get("entity_id"),
            values.get("return_url"),
            values.get("salesbot_token_jti"),
            values.get("salesbot_token_iat_text") or values.get("salesbot_token_iat"),
            _message_hash(normalized_message),
        )
    )
    digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()
    external_message_id = f"kommo:instagram_comment_callback:{digest}"
    correlation_id = f"kommo:instagram_comment:{digest[:32]}"
    return external_message_id, correlation_id


async def _discard_private_job_if_superseded_by_recent_comment(job: dict) -> bool:
    if not _is_instagram_private_message_job(job):
        return False
    normalized_message = _normalize_reconciliation_message(job.get("combined_message"))
    if not normalized_message:
        return False
    entity_sql, entity_values = _job_entity_match(job)
    message_hash = normalized_text_hash(normalized_message)
    window_seconds = _comment_mirror_window_seconds()
    lock_values = _job_reconciliation_values(job, normalized_message)
    async with db.get_db().transaction():
        await db.execute(
            "SELECT pg_advisory_xact_lock(hashtext(:reconciliation_key))",
            {"reconciliation_key": _comment_reconciliation_key(lock_values, _message_hash(normalized_message))},
        )
        match = await db.fetch_one(
            f"""
            SELECT candidate.id,
                   candidate.correlation_timestamp,
                   candidate.timestamp_source,
                   ABS(EXTRACT(EPOCH FROM (
                       candidate.correlation_timestamp - CAST(:private_created_at AS timestamptz)
                   ))) AS timestamp_delta_seconds
            FROM (
                SELECT comment_job.id,
                       COALESCE(
                           matched_meta_event.event_timestamp,
                           to_timestamp(
                               CASE
                                   WHEN comment_job.callback_claims ->> 'iat' ~ '^[0-9]+(\\.[0-9]+)?$'
                                   THEN CAST(comment_job.callback_claims ->> 'iat' AS double precision)
                                   ELSE NULL
                               END
                           ),
                           comment_job.created_at
                       ) AS correlation_timestamp,
                       CASE
                           WHEN matched_meta_event.event_timestamp IS NOT NULL THEN 'meta_event_timestamp'
                           WHEN comment_job.callback_claims ->> 'iat' ~ '^[0-9]+(\\.[0-9]+)?$' THEN 'salesbot_iat'
                           ELSE 'comment_job_created_at'
                       END AS timestamp_source
                FROM kommo_message_jobs comment_job
                LEFT JOIN LATERAL (
                    SELECT event.event_timestamp
                    FROM meta_instagram_context_events event
                    WHERE event.event_type = 'comment'
                      AND event.matched_kommo_job_id = comment_job.id
                      AND event.normalized_text_hash = :normalized_text_hash
                    ORDER BY event.event_timestamp ASC, event.id ASC
                    LIMIT 1
                ) matched_meta_event ON TRUE
                WHERE comment_job.interaction_type = 'instagram_comment'
                  AND comment_job.channel = 'instagram'
                  AND comment_job.status NOT IN ('discarded', 'failed')
                  AND {entity_sql}
                  AND {_normalized_message_sql('comment_job.combined_message')} = :normalized_message
                UNION ALL
                SELECT meta_event.id, meta_event.event_timestamp, 'meta_event_timestamp'
                FROM meta_instagram_context_events meta_event
                WHERE meta_event.event_type = 'comment'
                  AND meta_event.matched_kommo_job_id = CAST(:job_id AS uuid)
                  AND meta_event.normalized_text_hash = :normalized_text_hash
            ) candidate
            WHERE ABS(EXTRACT(EPOCH FROM (
                candidate.correlation_timestamp - CAST(:private_created_at AS timestamptz)
            ))) <= :window_seconds
            ORDER BY timestamp_delta_seconds ASC, candidate.id ASC
            LIMIT 1
            """,
            entity_values
            | {
                "job_id": str(job["id"]),
                "private_created_at": job.get("created_at"),
                "normalized_message": normalized_message,
                "normalized_text_hash": message_hash,
                "window_seconds": window_seconds,
            },
        )
        if not match:
            return False
        discarded = await db.fetch_one(
            """
            UPDATE kommo_message_jobs
            SET status = 'discarded',
                last_error = :reason,
                processing_started_at = NULL,
                processing_lease_id = NULL,
                completed_at = NOW(),
                updated_at = NOW()
            WHERE id = CAST(:job_id AS uuid)
              AND status IN (
                  'pending', 'prepared', 'processing', 'waiting_for_salesbot',
                  'waiting_for_context', 'ready'
              )
              AND continuation_payload IS NULL
              AND assistant_message_persisted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM kommo_outbound_deliveries outbound
                  WHERE outbound.job_id = kommo_message_jobs.id
                    AND outbound.transport = 'chats_api'
                    AND outbound.status IN ('sending', 'accepted', 'confirmed', 'delivery_unknown')
              )
            RETURNING id
            """,
            {"job_id": str(job["id"]), "reason": COMMENT_PRIVATE_SUPERSEDED_REASON},
        )
        if not discarded:
            return False
    logger.info(
        "Kommo Instagram private-message job discarded before direct processing because native comment job exists: job_id=%s comment_job_id=%s reason=%s",
        job.get("id"),
        match["id"],
        COMMENT_PRIVATE_SUPERSEDED_REASON,
    )
    return True


def _is_instagram_private_message_job(job: dict) -> bool:
    origin = str(job.get("origin") or "").lower()
    return _job_interaction_type(job) == "private_message" and (
        job.get("channel") == "instagram" or "instagram" in origin
    )


def _normalize_reconciliation_message(value) -> str:
    return normalize_message_text(value)


def _message_hash(normalized_message: str) -> str:
    return hashlib.sha256((normalized_message or "").encode("utf-8")).hexdigest()


def _comment_reconciliation_key(values: dict, message_hash: str) -> str:
    return ":".join(
        str(part or "")
        for part in (
            "kommo-comment-reconcile",
            values.get("entity_type"),
            values.get("entity_id"),
            message_hash,
        )
    )


def _normalized_message_sql(column: str) -> str:
    return f"LOWER(REGEXP_REPLACE(BTRIM(COALESCE({column}, '')), '\\s+', ' ', 'g'))"


def _signed_entity_match_sql(alias: str | None = None) -> str:
    prefix = f"{alias}." if alias else ""
    return f"""
    (
        (:entity_type = 'leads' AND {prefix}lead_id = :entity_id)
        OR (:entity_type = 'contacts' AND {prefix}contact_id = :entity_id)
    )
    """


def _job_entity_match(job: dict) -> tuple[str, dict]:
    clauses = []
    values = {}
    if job.get("lead_id"):
        clauses.append("comment_job.lead_id = CAST(:lead_id AS text)")
        values["lead_id"] = job.get("lead_id")
    if job.get("contact_id"):
        clauses.append("comment_job.contact_id = CAST(:contact_id AS text)")
        values["contact_id"] = job.get("contact_id")
    if clauses:
        return f"({' OR '.join(clauses)})", values
    return "FALSE", {}


def _job_reconciliation_values(job: dict, normalized_message: str) -> dict:
    if job.get("lead_id"):
        entity_type, entity_id = "leads", job.get("lead_id")
    else:
        entity_type, entity_id = "contacts", job.get("contact_id")
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "normalized_message": normalized_message,
    }


def _comment_mirror_window_seconds() -> int:
    configured = getattr(get_config(), "meta_context_match_window_seconds", None)
    try:
        return max(5, min(int(configured), 300))
    except (TypeError, ValueError):
        return COMMENT_MIRROR_RECONCILIATION_SECONDS


def _instagram_callback_origin(origin: str | None) -> str:
    text = str(origin or "").strip()
    if not text or (text.startswith("{{") and text.endswith("}}")):
        return "instagram"
    return text


def _safe_claims(claims: dict) -> dict:
    allowed = {"iss", "aud", "jti", "iat", "nbf", "exp", "account_id", "user_id", "subdomain", "client_uuid", "client_uid", "entity_id", "entity_type"}
    return {key: claims[key] for key in allowed if key in claims}


def _claim_as_str(claims: dict, key: str) -> str | None:
    value = claims.get(key)
    return str(value) if value not in (None, "") else None


def _failure_continuation_data(job: dict) -> tuple[dict, dict]:
    return _customer_message_continuation_data(job, CUSTOMER_DELIVERY_FAILURE_MESSAGE)


def _customer_message_continuation_data(job: dict, customer_message: str) -> tuple[dict, dict]:
    message, diagnostics = prepare_kommo_customer_message(
        customer_message,
        job.get("channel") or "whatsapp",
        {},
        interaction_type=_job_interaction_type(job),
    )
    return {"status": "fail", "message": message}, diagnostics


async def _continue_and_discard_job(
    client: KommoClient,
    job: dict,
    reason: str | None,
    *,
    send_customer_fallback: bool = False,
    customer_message: str | None = None,
    customer: dict | None = None,
) -> None:
    if _is_direct_instagram_dm(job):
        direct_message = customer_message
        if not direct_message and send_customer_fallback:
            direct_message = CUSTOMER_DELIVERY_FAILURE_MESSAGE
        if not direct_message:
            await _mark_job(
                job["id"],
                "discarded",
                reason,
                processing_lease_id=job.get("processing_lease_id"),
            )
            return
        try:
            delivery_result = await deliver_response(
                job={**job, "direct_delivery_purpose": "fallback"},
                result={},
                customer_text=direct_message,
                client=client,
            )
            if not await _mark_direct_job_waiting_for_delivery(
                job["id"],
                job.get("processing_lease_id"),
                str(job.get("talk_id") or ""),
                delivery_result.provider_message_ids,
                _pending_assistant_payload(
                    customer,
                    {},
                    delivery_result.customer_text,
                    [],
                    final_status="discarded",
                ),
                reason=reason,
            ):
                raise KommoDeliveryStateError(
                    "Kommo direct Instagram fallback could not enter delivery reconciliation"
                )
        except KommoDeliveryAbortedError as error:
            logger.info(
                "Kommo direct Instagram fallback aborted before network send: job_id=%s reason=%s",
                job["id"],
                sanitize_job_error(error),
            )
            await _mark_job(
                job["id"],
                "failed",
                sanitize_job_error(error),
                processing_lease_id=job.get("processing_lease_id"),
            )
        except (
            KommoDeliveryUnknownError,
            KommoPartialDeliveryError,
            KommoDeliveryStateError,
        ) as error:
            await _mark_job(
                job["id"],
                "delivery_unknown",
                sanitize_job_error(error),
                processing_lease_id=job.get("processing_lease_id"),
            )
        except Exception as error:
            await _mark_job(
                job["id"],
                "failed",
                sanitize_job_error(error),
                processing_lease_id=job.get("processing_lease_id"),
            )
        return

    continuation_started = False
    try:
        if customer_message:
            continuation_data, message_diagnostics = _customer_message_continuation_data(
                job,
                customer_message,
            )
        elif send_customer_fallback:
            continuation_data, message_diagnostics = _failure_continuation_data(job)
        else:
            continuation_data = {"status": "fail", "message": ""}
            message_diagnostics = build_kommo_message_diagnostics(
                "",
                job.get("channel") or "whatsapp",
                "preserve",
                interaction_type=_job_interaction_type(job),
            )
        logger.info("Kommo continuing Salesbot with failure status: job_id=%s reason=%s", job["id"], reason)
        if not await _mark_job_continuing(job["id"], job.get("processing_lease_id"), continuation_data):
            logger.warning("Kommo job lost its processing lease before failure continuation: job_id=%s", job["id"])
            return
        continuation_started = True
        _log_continuation_prepared(
            job["id"],
            continuation_data,
            message_diagnostics,
        )
        try:
            response_payload = await client.continue_salesbot(
                job["return_url"],
                data=continuation_data,
            )
        except KommoAPIError as e:
            logger.warning(
                "Kommo failure continuation failed: job_id=%s interaction_type=%s error=%s",
                job["id"],
                _job_interaction_type(job),
                sanitize_job_error(e),
            )
            raise
        except Exception as e:
            logger.warning(
                "Kommo failure continuation failed: job_id=%s interaction_type=%s error=%s",
                job["id"],
                _job_interaction_type(job),
                sanitize_job_error(e),
            )
            raise
        logger.info(
            "Kommo failure continuation succeeded: job_id=%s interaction_type=%s",
            job["id"],
            _job_interaction_type(job),
        )
        await _mark_job_discarded(job["id"], job.get("processing_lease_id"), reason, response_payload)
    except KommoAPIError as e:
        status = _status_after_continuation_error(e, continuation_started)
        logger.warning(
            "Kommo failure continuation rejected: job_id=%s status=%s error=%s",
            job["id"],
            status,
            sanitize_job_error(e),
        )
        await _mark_job(
            job["id"],
            status,
            sanitize_job_error(e),
            processing_lease_id=job.get("processing_lease_id"),
        )
    except Exception as e:
        await _mark_job(
            job["id"],
            "delivery_unknown" if continuation_started else "failed",
            sanitize_job_error(e),
            processing_lease_id=job.get("processing_lease_id"),
        )


async def _mark_job_continuing(
    job_id: str,
    processing_lease_id: str | None,
    continuation_data: dict,
) -> bool:
    continuation_payload = {"data": continuation_data}
    updated = await db.fetch_one(
        """
        UPDATE kommo_message_jobs
        SET status = 'continuing',
            continuation_payload = CAST(:continuation_payload AS jsonb),
            processing_started_at = NOW(),
            updated_at = NOW()
        WHERE id = :id
          AND status = 'processing'
          AND processing_lease_id = CAST(:processing_lease_id AS uuid)
        RETURNING id
        """,
        {
            "id": job_id,
            "processing_lease_id": processing_lease_id,
            "continuation_payload": json.dumps(continuation_payload),
        },
    )
    return bool(updated)


async def _retry_ready_job(job: dict, error: str) -> None:
    await db.execute(
        """
        UPDATE kommo_message_jobs
        SET status = 'ready',
            last_error = :last_error,
            processing_started_at = NULL,
            processing_lease_id = NULL,
            ai_started_at = NULL,
            updated_at = NOW()
        WHERE id = :id
          AND status = 'processing'
          AND processing_lease_id = CAST(:processing_lease_id AS uuid)
        """,
        {
            "id": job["id"],
            "processing_lease_id": job.get("processing_lease_id"),
            "last_error": sanitize_job_error(error),
        },
    )


async def _fail_ready_job(
    client: KommoClient,
    job: dict,
    error: str,
    *,
    customer: dict | None = None,
) -> None:
    if _is_direct_instagram_dm(job):
        if customer is None:
            await _mark_job(
                job["id"],
                "failed",
                error,
                processing_lease_id=job.get("processing_lease_id"),
            )
            return
        await _continue_and_discard_job(
            client,
            job,
            error,
            send_customer_fallback=True,
            customer=customer,
        )
        return
    if job.get("return_url"):
        continuation_data, message_diagnostics = _failure_continuation_data(job)
        if await _mark_job_continuing(job["id"], job.get("processing_lease_id"), continuation_data):
            try:
                _log_continuation_prepared(job["id"], continuation_data, message_diagnostics)
                await client.continue_salesbot(job["return_url"], data=continuation_data)
            except Exception as continuation_error:
                await _mark_job(
                    job["id"],
                    "delivery_unknown",
                    sanitize_job_error(continuation_error),
                    processing_lease_id=job.get("processing_lease_id"),
                )
                return
    await _mark_job(
        job["id"],
        "failed",
        error,
        processing_lease_id=job.get("processing_lease_id"),
    )


async def _mark_job_sent(job_id: str, processing_lease_id: str | None, response_payload) -> bool:
    updated = await db.fetch_one(
        """
        UPDATE kommo_message_jobs
        SET status = 'sent',
            last_error = NULL,
            continuation_response = CAST(:continuation_response AS jsonb),
            processing_started_at = NULL,
            processing_lease_id = NULL,
            completed_at = NOW(),
            updated_at = NOW()
        WHERE id = :id
          AND status = 'continuing'
          AND processing_lease_id = CAST(:processing_lease_id AS uuid)
        RETURNING id
        """,
        {
            "id": job_id,
            "processing_lease_id": processing_lease_id,
            "continuation_response": json.dumps({"response": response_payload}),
        },
    )
    if updated:
        logger.info("Kommo continuation accepted: job_id=%s", job_id)
    return bool(updated)


async def _mark_direct_job_waiting_for_delivery(
    job_id: str,
    processing_lease_id: str | None,
    talk_id: str,
    provider_message_ids: list[str],
    pending_assistant_message: dict,
    *,
    reason: str | None = None,
) -> bool:
    updated = await db.fetch_one(
        """
        UPDATE kommo_message_jobs
        SET status = 'waiting_for_delivery',
            last_error = :last_error,
            continuation_response = CAST(:delivery_response AS jsonb),
            pending_assistant_message = CAST(:pending_assistant_message AS jsonb),
            delivery_wait_started_at = NOW(),
            delivery_reconcile_after_at = NOW(),
            delivery_reconcile_attempt_count = 0,
            processing_started_at = NULL,
            processing_lease_id = NULL,
            completed_at = NULL,
            updated_at = NOW()
        WHERE id = :id
          AND status = 'processing'
          AND processing_lease_id = CAST(:processing_lease_id AS uuid)
          AND EXISTS (
              SELECT 1
              FROM kommo_outbound_deliveries outbound
              WHERE outbound.job_id = kommo_message_jobs.id
                AND outbound.transport = 'chats_api'
                AND outbound.status IN ('accepted', 'confirmed')
                AND outbound.provider_message_id = ANY(CAST(:provider_message_ids AS text[]))
          )
        RETURNING id
        """,
        {
            "id": job_id,
            "processing_lease_id": processing_lease_id,
            "last_error": sanitize_job_error(reason) if reason else None,
            "pending_assistant_message": json.dumps(
                pending_assistant_message,
                ensure_ascii=False,
            ),
            "provider_message_ids": provider_message_ids,
            "delivery_response": json.dumps(
                {
                    "transport": "chats_api",
                    "provider_message_ids": provider_message_ids,
                }
            ),
        },
    )
    if updated:
        logger.info(
            "Kommo direct Instagram delivery awaiting reconciliation: job_id=%s "
            "talk_id=%s provider_message_id=%s provider_delivery_status=accepted",
            job_id,
            talk_id,
            ",".join(provider_message_ids),
        )
    return bool(updated)


def _pending_assistant_payload(
    customer: dict | None,
    result: dict,
    customer_text: str | None,
    delivered_attachments: list[dict],
    *,
    final_status: str,
) -> dict:
    return {
        "customer_id": str(customer["id"]) if customer and customer.get("id") else None,
        "channel": "instagram",
        "content": customer_text or result.get("text") or "",
        "function_calls": result.get("function_calls"),
        "attachments": delivered_attachments,
        "final_status": final_status,
    }


def _log_continuation_prepared(
    job_id: str,
    continuation_data: dict,
    message_diagnostics: dict | None = None,
) -> None:
    message = str(continuation_data.get("message") or "")
    diagnostics = message_diagnostics or build_kommo_message_diagnostics(message, None, False)
    logger.info(
        "Kommo continuation prepared: job_id=%s status=%s channel=%s interaction_type=%s message_present=%s message_length=%s newline_count=%s non_ascii_present=%s emoji_present=%s replacement_char_present=%s literal_question_mark_present=%s kommo_emoji_mode=%s kommo_strip_emoji_applied=%s",
        job_id,
        continuation_data.get("status"),
        diagnostics["channel"],
        diagnostics["interaction_type"],
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


async def _mark_job_discarded(
    job_id: str,
    processing_lease_id: str | None,
    reason: str | None,
    response_payload,
) -> bool:
    updated = await db.fetch_one(
        """
        UPDATE kommo_message_jobs
        SET status = 'discarded',
            last_error = :last_error,
            continuation_response = CAST(:continuation_response AS jsonb),
            processing_started_at = NULL,
            processing_lease_id = NULL,
            completed_at = NOW(),
            updated_at = NOW()
        WHERE id = :id
          AND status = 'continuing'
          AND processing_lease_id = CAST(:processing_lease_id AS uuid)
        RETURNING id
        """,
        {
            "id": job_id,
            "processing_lease_id": processing_lease_id,
            "last_error": sanitize_job_error(reason) if reason else None,
            "continuation_response": json.dumps({"response": response_payload}),
        },
    )
    if updated:
        logger.info("Kommo job marked discarded: job_id=%s reason=%s", job_id, reason)
    return bool(updated)


async def _store_assistant_message_after_delivery(
    customer: dict,
    job: dict,
    result: dict,
    customer_text: str | None,
    *,
    delivered_attachments: list[dict] | None = None,
    expected_job_status: str | None = "continuing",
) -> None:
    if expected_job_status not in {None, "processing", "continuing"}:
        raise ValueError("Unsupported Kommo assistant persistence job status")
    content = customer_text or result.get("text") or ""
    attachments = delivered_attachments or []
    if not content and not attachments:
        return
    status_condition = (
        "AND status = :expected_job_status "
        "AND processing_lease_id = CAST(:processing_lease_id AS uuid)"
        if expected_job_status is not None
        else ""
    )
    values = {"id": job["id"]}
    if expected_job_status is not None:
        values.update(
            {
                "processing_lease_id": job.get("processing_lease_id"),
                "expected_job_status": expected_job_status,
            }
        )
    async with db.get_db().transaction():
        existing = await db.fetch_one(
            f"""
            SELECT assistant_message_persisted_at
            FROM kommo_message_jobs
            WHERE id = :id
              {status_condition}
            FOR UPDATE
            """,
            values,
        )
        if not existing:
            reason = "job_missing" if expected_job_status is None else "processing_lease_lost"
            logger.warning(
                "Kommo assistant history skipped: job_id=%s reason=%s",
                job["id"],
                reason,
            )
            return
        if existing and existing["assistant_message_persisted_at"]:
            logger.info("Kommo assistant history already persisted: job_id=%s", job["id"])
            return
        await conversations.store_message(
            customer_id=customer["id"],
            role="assistant",
            content=content,
            channel=job.get("channel") or customer.get("channel") or "whatsapp",
            function_calls=result.get("function_calls"),
            source_id=_conversation_source_id(job),
            attachments=attachments or None,
            interaction_type=_job_interaction_type(job),
        )
        await db.execute(
            f"""
            UPDATE kommo_message_jobs
            SET assistant_message_persisted_at = NOW(),
                updated_at = NOW()
            WHERE id = :id
              {status_condition}
              AND assistant_message_persisted_at IS NULL
            """,
            values,
        )
    logger.info("Kommo assistant history persisted: job_id=%s", job["id"])


def _semantic_attachments_from_result(result: dict) -> list[dict]:
    attachments = []
    product_image = result.get("product_image")
    if isinstance(product_image, dict) and product_image.get("type") == "product_image":
        attachment = {"type": "product_image"}
        for field in ("product_name", "sku"):
            value = product_image.get(field)
            if isinstance(value, str) and value.strip():
                attachment[field] = value.strip()
        attachments.append(attachment)

    catalog_pdf = result.get("catalog_pdf")
    if isinstance(catalog_pdf, dict) and catalog_pdf.get("type") == "catalog_pdf":
        attachment = {"type": "catalog_pdf"}
        for field in ("filename", "catalog_fingerprint"):
            value = catalog_pdf.get(field)
            if isinstance(value, str) and value.strip():
                attachment[field] = value.strip()
        attachments.append(attachment)
    return attachments


def _status_after_continuation_error(error: KommoAPIError, continuation_started: bool) -> str:
    if not continuation_started:
        return "failed"
    if error.status_code is None or error.status_code in _TRANSIENT_CONTINUATION_STATUSES:
        return "delivery_unknown"
    return "failed"


async def _mark_job(
    job_id: str,
    status: str,
    error: str | None,
    *,
    processing_lease_id: str | None = None,
) -> None:
    lease_condition = (
        "AND processing_lease_id = CAST(:processing_lease_id AS uuid)"
        if processing_lease_id is not None
        else ""
    )
    await db.execute(
        f"""
        UPDATE kommo_message_jobs
        SET status = :status,
            last_error = :last_error,
            processing_started_at = NULL,
            processing_lease_id = NULL,
            completed_at = CASE WHEN :status IN ('sent', 'discarded', 'failed', 'delivery_unknown') THEN NOW() ELSE completed_at END,
            updated_at = NOW()
        WHERE id = :id
          {lease_condition}
        """,
        {
            "status": status,
            "last_error": sanitize_job_error(error) if error else None,
            "id": job_id,
            **({"processing_lease_id": processing_lease_id} if processing_lease_id is not None else {}),
        },
    )
    logger.info("Kommo job marked %s: job_id=%s reason=%s", status, job_id, sanitize_job_error(error) if error else None)


async def _store_user_message_if_suppressed(customer: dict, job: dict) -> None:
    await conversations.store_message(
        customer_id=customer["id"],
        role="user",
        content=job["combined_message"],
        channel=job.get("channel") or customer.get("channel") or "whatsapp",
        media_url=job.get("media_url"),
        source_id=_conversation_source_id(job),
        interaction_type=_job_interaction_type(job),
    )


def _conversation_source_id(job: dict) -> str:
    return f"kommo-job:{job['id']}"


def _event_values(event: NormalizedKommoEvent, external_message_id: str, text: str) -> dict:
    return {
        "correlation_id": event.correlation_id,
        "external_message_id": external_message_id,
        "lead_id": event.lead_id,
        "contact_id": event.contact_id,
        "chat_id": event.chat_id,
        "talk_id": event.talk_id,
        "author_id": event.author_id,
        "author_name": event.author_name,
        "author_username": event.author_username,
        "author_profile_url": event.author_profile_url,
        "sender_username": event.sender_username,
        "sender_profile_url": event.sender_profile_url,
        "origin": event.origin,
        "channel": event.channel,
        "interaction_type": event.interaction_type,
        "combined_message": text,
        "media_url": event.media_url,
        "message_type": event.message_type,
        "inbound_attachments": json.dumps(_event_inbound_attachments(event, external_message_id)),
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
            talk_id, author_id, origin, channel, interaction_type, receipt_status, received_at,
            message_text, normalized_text_hash
        ) VALUES (
            :external_message_id, :job_id, :correlation_id, :lead_id, :contact_id, :chat_id,
            :talk_id, :author_id, :origin, :channel, :interaction_type, :receipt_status, :received_at,
            :message_text, :normalized_text_hash
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
            "author_id": event.author_id,
            "origin": event.origin,
            "channel": event.channel,
            "interaction_type": event.interaction_type,
            "receipt_status": receipt_status,
            "received_at": event.created_at,
            "message_text": (
                _message_placeholder(event, external_message_id)
                if _is_audio_message_type(event.message_type)
                else (event.text or "").strip() or _message_placeholder(event, external_message_id)
            ),
            "normalized_text_hash": normalized_text_hash(
                _message_placeholder(event, external_message_id)
                if _is_audio_message_type(event.message_type)
                else (event.text or "").strip() or _message_placeholder(event, external_message_id)
            ),
        },
    )


def _message_placeholder(event: NormalizedKommoEvent, external_message_id: str | None = None) -> str:
    if _is_audio_message_type(event.message_type):
        return _audio_placeholder(event.message_type, external_message_id or event.message_id)
    if event.media_url:
        return "El cliente envio una imagen por Kommo."
    if event.message_type:
        return f"[Mensaje de tipo no soportado por Kommo: {event.message_type}]"
    return "[Mensaje recibido sin texto por Kommo]"


def _has_unsupported_attachment(job: dict) -> bool:
    message_type = str(job.get("message_type") or "").strip().lower()
    return bool(message_type) and message_type not in _AUDIO_MESSAGE_TYPES | _IMAGE_MESSAGE_TYPES | {"text"}


async def _effective_customer_message(job: dict) -> str:
    combined_message = str(job.get("combined_message") or "").strip()
    attachments = _job_inbound_audio_attachments(job)
    if attachments:
        effective_message = combined_message
        for attachment in attachments:
            media_url = str(attachment.get("media_url") or "").strip()
            if not media_url:
                raise AudioTranscriptionError("Audio attachment URL is missing", retryable=False)
            transcription = (await transcribe_audio_url(media_url)).strip()
            if not transcription:
                raise AudioTranscriptionError("Audio transcription was empty", retryable=False)
            placeholder = _audio_placeholder(
                attachment.get("message_type"),
                attachment.get("external_message_id"),
            )
            if placeholder not in effective_message:
                raise AudioTranscriptionError("Audio attachment placeholder is missing", retryable=False)
            effective_message = effective_message.replace(placeholder, transcription, 1)
        return effective_message

    # Jobs created before inbound_attachments was introduced retain the original one-URL behavior.
    if not _is_audio_job(job):
        return combined_message

    media_url = str(job.get("media_url") or "").strip()
    if not media_url:
        raise AudioTranscriptionError("Audio attachment URL is missing", retryable=False)
    transcription = (await transcribe_audio_url(media_url)).strip()
    if not transcription:
        raise AudioTranscriptionError("Audio transcription was empty", retryable=False)

    for message_type in _AUDIO_MESSAGE_TYPES:
        placeholder = _audio_placeholder(message_type)
        if placeholder in combined_message:
            # One URL is retained per debounce job; separate attachment storage is needed for multiple voice notes.
            return combined_message.replace(placeholder, transcription, 1)
    return "\n".join(part for part in (combined_message, transcription) if part)


def _is_audio_job(job: dict) -> bool:
    return _is_audio_message_type(job.get("message_type"))


def _is_audio_message_type(message_type: object) -> bool:
    return str(message_type or "").strip().lower() in _AUDIO_MESSAGE_TYPES


def _audio_placeholder(message_type: object, external_message_id: object | None = None) -> str:
    normalized = str(message_type or "voice").strip().lower()
    if external_message_id:
        return f"[Kommo {normalized}:{str(external_message_id).strip()} pendiente de transcripcion]"
    return f"[Kommo {normalized} pendiente de transcripcion]"


def _event_inbound_attachments(event: NormalizedKommoEvent, external_message_id: str) -> list[dict[str, str | None]]:
    normalized_type = str(event.message_type or "").strip().lower()
    if normalized_type not in _AUDIO_MESSAGE_TYPES | _IMAGE_MESSAGE_TYPES:
        return []
    return [{
        "external_message_id": external_message_id,
        "message_type": normalized_type,
        "media_url": event.media_url,
    }]


def _merged_legacy_media(
    pending_media_url: str | None,
    pending_message_type: str | None,
    event: NormalizedKommoEvent,
) -> tuple[str | None, str | None]:
    if event.media_url:
        return event.media_url, event.message_type or pending_message_type
    return pending_media_url, pending_message_type or event.message_type


def _job_inbound_audio_attachments(job: dict) -> list[dict]:
    return [
        item
        for item in _job_inbound_attachments(job)
        if _is_audio_message_type(item.get("message_type"))
    ]


def _job_image_media_url(job: dict) -> str | None:
    # generate_response accepts one image, so intentionally analyze the latest image in the debounce batch.
    for item in reversed(_job_inbound_attachments(job)):
        if str(item.get("message_type") or "").strip().lower() not in _IMAGE_MESSAGE_TYPES:
            continue
        media_url = str(item.get("media_url") or "").strip()
        if media_url:
            return media_url
    if _is_audio_job(job):
        return None
    return job.get("media_url")


def _job_inbound_attachments(job: dict) -> list[dict]:
    value = job.get("inbound_attachments") or []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise AudioTranscriptionError("Inbound attachment metadata is invalid", retryable=False) from error
    if not isinstance(value, list):
        raise AudioTranscriptionError("Inbound attachment metadata is invalid", retryable=False)

    attachments = []
    seen_message_ids: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        message_type = str(item.get("message_type") or "").strip().lower()
        if message_type not in _AUDIO_MESSAGE_TYPES | _IMAGE_MESSAGE_TYPES:
            continue
        external_message_id = str(item.get("external_message_id") or "").strip()
        if not external_message_id:
            raise AudioTranscriptionError("Inbound attachment metadata is invalid", retryable=False)
        if external_message_id in seen_message_ids:
            continue
        seen_message_ids.add(external_message_id)
        attachments.append(item)
    return attachments


def _local_sender_id(job: dict) -> str:
    return str(job.get("chat_id") or job.get("contact_id") or job.get("lead_id") or job.get("correlation_id"))


def _job_log_context(job: dict) -> dict:
    return {
        "job_id": str(job.get("id")) if job.get("id") is not None else None,
        "status": job.get("status"),
        "channel": job.get("channel"),
        "interaction_type": _job_interaction_type(job),
        "origin": job.get("origin"),
        "lead_id": job.get("lead_id"),
        "contact_id": job.get("contact_id"),
        "chat_id": job.get("chat_id"),
        "talk_id": job.get("talk_id"),
        "author_id": job.get("author_id"),
        "has_author_name": bool(job.get("author_name")),
        "has_author_username": bool(job.get("author_username")),
        "has_author_profile_url": bool(job.get("author_profile_url")),
        "has_sender_username": bool(job.get("sender_username")),
        "has_sender_profile_url": bool(job.get("sender_profile_url")),
        "has_message": bool(job.get("combined_message")),
        "has_media": bool(job.get("media_url")),
        "has_return_url": bool(job.get("return_url")),
        "has_public_comment_context": bool(_job_public_comment_context(job)),
        "context_status": job.get("context_status"),
        "has_meta_context_event": bool(job.get("meta_context_event_id")),
        "context_correlation_score": job.get("context_correlation_score"),
        "context_deadline_at": _timestamp_for_log(job.get("context_deadline_at")),
        "attempt_count": job.get("attempt_count"),
        "seconds_until_due": float(job["seconds_until_due"]) if job.get("seconds_until_due") is not None else None,
        "salesbot_launched_at": _timestamp_for_log(job.get("salesbot_launched_at")),
        "processing_started_at": _timestamp_for_log(job.get("processing_started_at")),
        "created_at": _timestamp_for_log(job.get("created_at")),
        "updated_at": _timestamp_for_log(job.get("updated_at")),
    }


def _job_interaction_type(job: dict) -> str:
    interaction_type = str(job.get("interaction_type") or "private_message").strip().lower()
    return interaction_type if interaction_type in {"private_message", "instagram_comment"} else "private_message"


def _is_direct_instagram_dm(job: dict) -> bool:
    return (
        job.get("channel") == "instagram"
        and str(job.get("interaction_type") or "").strip().lower() == "private_message"
    )


def _is_valid_kommo_id(value) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    text = str(value).strip()
    return text.isdigit() and int(text) > 0


def _salesbot_id_for_channel(channel: str | None) -> int:
    config = get_config()
    normalized_channel = str(channel or "").strip().lower()
    if normalized_channel == "instagram":
        raise KommoAPIError("Instagram private messages do not use Salesbot")
    if normalized_channel == "whatsapp":
        salesbot_id = config.kommo_whatsapp_salesbot_id or config.kommo_salesbot_id
    else:
        raise KommoAPIError("Unsupported Kommo private-message channel")
    if not isinstance(salesbot_id, int) or isinstance(salesbot_id, bool) or salesbot_id <= 0:
        raise KommoAPIError(f"Kommo Salesbot ID is not configured for {normalized_channel}")
    return salesbot_id


def _job_public_comment_context(job: dict) -> dict:
    context = job.get("public_comment_context")
    if isinstance(context, dict):
        return context
    if isinstance(context, str) and context.strip():
        try:
            loaded = json.loads(context)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _job_instagram_content_context(job: dict) -> dict:
    context = job.get("instagram_content_context")
    if isinstance(context, dict):
        return context
    if isinstance(context, str) and context.strip():
        try:
            loaded = json.loads(context)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _is_resolved_story_context(context: dict) -> bool:
    return bool(
        context.get("source") == "story_reply"
        and context.get("mapping_status") == "resolved"
        and context.get("story_id")
        and context.get("product_skus")
    )


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
