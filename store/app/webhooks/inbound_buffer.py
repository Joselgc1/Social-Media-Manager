"""Durable debounce queue for Meta inbound messages."""

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable

from app import db
from app.channels.meta_errors import MetaSendError
from app.config import get_config

logger = logging.getLogger(__name__)

MESSAGE_DEBOUNCE_SECONDS = 15
PROCESSING_LEASE_MINUTES = 5
PROCESSING_HEARTBEAT_SECONDS = 30
MAX_PROCESSING_ATTEMPTS = 5
MAX_META_SEND_ATTEMPTS = 3
MAX_CONCURRENT_INBOUND_PROCESSORS = 3
_inbound_processor_semaphore = asyncio.Semaphore(MAX_CONCURRENT_INBOUND_PROCESSORS)

Processor = Callable[[str, str, str | None, dict, str, str, str, dict], Awaitable[None]]


def _merge_profile(current: dict, new_values: dict | None) -> dict:
    merged = dict(current or {})
    for key, value in (new_values or {}).items():
        if value:
            merged[key] = value
    return merged


def _combine_parts(parts: list[str]) -> str:
    cleaned = [part.strip() for part in parts if (part or "").strip()]
    return "\n".join(cleaned)


async def enqueue_inbound_message(
    *,
    channel: str,
    sender_id: str,
    message_id: str,
    text: str,
    media_url: str | None = None,
    customer_profile: dict | None = None,
    interaction_type: str = "private_message",
    integration_context: dict | None = None,
) -> bool:
    """Persist one Meta delivery and extend its customer's debounce window."""
    if channel not in {"whatsapp", "instagram"}:
        raise ValueError("Unsupported inbound channel.")
    if not sender_id or not message_id:
        raise ValueError("Inbound messages require sender_id and message_id.")
    if interaction_type not in {"private_message", "instagram_comment"}:
        raise ValueError("Unsupported inbound interaction type.")
    if channel != "instagram" and interaction_type != "private_message":
        raise ValueError("Only Instagram supports non-private Meta interactions.")
    integration_context = dict(integration_context or {})
    mergeable = interaction_type == "private_message" and not integration_context

    async with db.get_db().transaction():
        await _lock_sender(channel, sender_id)
        duplicate = await db.fetch_one(
            """
            SELECT id
            FROM meta_inbound_receipts
            WHERE channel = :channel AND external_message_id = :message_id
            """,
            {"channel": channel, "message_id": message_id},
        )
        if duplicate:
            return False

        pending = None
        if mergeable:
            pending = await db.fetch_one(
                """
                SELECT id, message_parts, customer_profile
                FROM meta_inbound_jobs
                WHERE channel = :channel
                  AND sender_id = :sender_id
                  AND interaction_type = 'private_message'
                  AND integration_context = '{}'::jsonb
                  AND status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE
                """,
                {"channel": channel, "sender_id": sender_id},
            )
        if pending:
            parts = _json_list(pending["message_parts"])
            parts.append(text)
            profile = _merge_profile(_json_dict(pending["customer_profile"]), customer_profile)
            job_id = str(pending["id"])
            await db.execute(
                """
                UPDATE meta_inbound_jobs
                SET message_parts = CAST(:parts AS jsonb),
                    media_url = COALESCE(:media_url, media_url),
                    customer_profile = CAST(:profile AS jsonb),
                    available_at = NOW() + (:delay * INTERVAL '1 second'),
                    updated_at = NOW()
                WHERE id = :job_id
                """,
                {
                    "parts": json.dumps(parts, ensure_ascii=False),
                    "media_url": media_url,
                    "profile": json.dumps(profile, ensure_ascii=False),
                    "delay": MESSAGE_DEBOUNCE_SECONDS,
                    "job_id": job_id,
                },
            )
        else:
            job = await db.fetch_one(
                """
                INSERT INTO meta_inbound_jobs (
                    channel, sender_id, message_parts, media_url,
                    customer_profile, interaction_type, integration_context, available_at
                )
                VALUES (
                    :channel, :sender_id, CAST(:parts AS jsonb), :media_url,
                    CAST(:profile AS jsonb), :interaction_type,
                    CAST(:integration_context AS jsonb),
                    NOW() + (:delay * INTERVAL '1 second')
                )
                RETURNING id
                """,
                {
                    "channel": channel,
                    "sender_id": sender_id,
                    "parts": json.dumps([text], ensure_ascii=False),
                    "media_url": media_url,
                    "profile": json.dumps(customer_profile or {}, ensure_ascii=False),
                    "interaction_type": interaction_type,
                    "integration_context": json.dumps(integration_context, ensure_ascii=False),
                    "delay": MESSAGE_DEBOUNCE_SECONDS,
                },
            )
            job_id = str(job["id"])

        await db.execute(
            """
            INSERT INTO meta_inbound_receipts (channel, external_message_id, job_id)
            VALUES (:channel, :message_id, :job_id)
            """,
            {"channel": channel, "message_id": message_id, "job_id": job_id},
        )

    if getattr(get_config(), "outbound_processing_enabled", True):
        asyncio.create_task(_process_after_delay(channel, sender_id))
    return True


async def process_due_inbound_jobs(limit: int = 10) -> int:
    """Lease and process due batches. Safe to call concurrently."""
    if not getattr(get_config(), "outbound_processing_enabled", True):
        return 0
    await recover_stale_inbound_jobs()
    tasks = []
    for _ in range(min(limit, MAX_CONCURRENT_INBOUND_PROCESSORS)):
        await _inbound_processor_semaphore.acquire()
        try:
            job = await _claim_due_job()
        except Exception:
            _inbound_processor_semaphore.release()
            raise
        if not job:
            _inbound_processor_semaphore.release()
            break
        tasks.append(asyncio.create_task(_process_claimed_with_slot(job)))
    if tasks:
        await asyncio.gather(*tasks)
    return len(tasks)


async def recover_stale_inbound_jobs() -> int:
    """Return abandoned processing leases to pending without losing newer messages."""
    stale_rows = await db.fetch_all(
        """
        SELECT id, channel, sender_id, processing_lease_token,
               COALESCE(processing_heartbeat_at, processing_started_at) AS lease_heartbeat_at
        FROM meta_inbound_jobs
        WHERE status = 'processing'
          AND COALESCE(processing_heartbeat_at, processing_started_at) < NOW() - (:minutes * INTERVAL '1 minute')
        ORDER BY COALESCE(processing_heartbeat_at, processing_started_at) ASC
        LIMIT 20
        """,
        {"minutes": PROCESSING_LEASE_MINUTES},
    )
    recovered = 0
    for stale in stale_rows:
        async with db.get_db().transaction():
            channel = stale["channel"]
            sender_id = stale["sender_id"]
            await _lock_sender(channel, sender_id)
            stale_job = await db.fetch_one(
                """
                SELECT id, message_parts, media_url, customer_profile, attempt_count,
                       interaction_type, integration_context,
                       outbound_started_at, outbound_message_ids, processing_lease_token
                FROM meta_inbound_jobs
                WHERE id = :job_id
                  AND status = 'processing'
                  AND processing_lease_token IS NOT DISTINCT FROM :lease_token
                  AND COALESCE(processing_heartbeat_at, processing_started_at)
                      < NOW() - (:minutes * INTERVAL '1 minute')
                FOR UPDATE
                """,
                {
                    "job_id": str(stale["id"]),
                    "lease_token": _record_value(stale, "processing_lease_token"),
                    "minutes": PROCESSING_LEASE_MINUTES,
                },
            )
            if not stale_job:
                continue
            outbound_ids = _json_list(_record_value(stale_job, "outbound_message_ids", []))
            if outbound_ids:
                await db.execute(
                    """
                    UPDATE meta_inbound_jobs
                    SET status = 'completed', completed_at = NOW(),
                        processing_started_at = NULL, processing_heartbeat_at = NULL,
                        processing_lease_token = NULL, last_error = 'stale_lease_completed_after_meta_accept',
                        updated_at = NOW()
                    WHERE id = :job_id
                    """,
                    {"job_id": str(stale_job["id"])},
                )
                recovered += 1
                continue
            if _record_value(stale_job, "outbound_started_at") is not None:
                await db.execute(
                    """
                    UPDATE meta_inbound_jobs
                    SET status = 'failed', processing_started_at = NULL,
                        processing_heartbeat_at = NULL, processing_lease_token = NULL,
                        last_error = 'delivery_unknown_after_stale_lease', updated_at = NOW()
                    WHERE id = :job_id
                    """,
                    {"job_id": str(stale_job["id"])},
                )
                recovered += 1
                continue
            stale_context = _json_dict(_record_value(stale_job, "integration_context", {}))
            pending = None
            if (
                _record_value(stale_job, "interaction_type", "private_message") == "private_message"
                and not stale_context
            ):
                pending = await db.fetch_one(
                    """
                    SELECT id, message_parts, media_url, customer_profile
                    FROM meta_inbound_jobs
                    WHERE channel = :channel AND sender_id = :sender_id
                      AND interaction_type = 'private_message'
                      AND integration_context = '{}'::jsonb
                      AND status = 'pending'
                    ORDER BY created_at ASC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    {"channel": channel, "sender_id": sender_id},
                )
            if pending:
                merged_parts = _json_list(stale_job["message_parts"]) + _json_list(pending["message_parts"])
                merged_profile = _merge_profile(
                    _json_dict(stale_job["customer_profile"]),
                    _json_dict(pending["customer_profile"]),
                )
                await db.execute(
                    """
                    UPDATE meta_inbound_jobs
                    SET message_parts = CAST(:parts AS jsonb),
                        media_url = COALESCE(media_url, :stale_media_url),
                        customer_profile = CAST(:profile AS jsonb),
                        available_at = NOW(),
                        updated_at = NOW()
                    WHERE id = :pending_id
                    """,
                    {
                        "parts": json.dumps(merged_parts, ensure_ascii=False),
                        "stale_media_url": stale_job["media_url"],
                        "profile": json.dumps(merged_profile, ensure_ascii=False),
                        "pending_id": str(pending["id"]),
                    },
                )
                await db.execute(
                    """
                    UPDATE meta_inbound_jobs
                    SET status = 'failed', processing_started_at = NULL,
                        processing_heartbeat_at = NULL, processing_lease_token = NULL,
                        last_error = 'stale_lease_merged', updated_at = NOW()
                    WHERE id = :job_id
                    """,
                    {"job_id": str(stale_job["id"])},
                )
            else:
                await db.execute(
                    """
                    UPDATE meta_inbound_jobs
                    SET status = 'pending', available_at = NOW(),
                        processing_started_at = NULL, processing_heartbeat_at = NULL,
                        processing_lease_token = NULL, last_error = 'stale_lease_recovered',
                        updated_at = NOW()
                    WHERE id = :job_id
                    """,
                    {"job_id": str(stale_job["id"])},
                )
            recovered += 1
    return recovered


async def cleanup_completed_inbound_jobs(retention_days: int = 7) -> None:
    """Bound durable queue growth; dedup receipts cascade with deleted jobs."""
    await db.execute(
        """
        DELETE FROM meta_inbound_jobs
        WHERE status IN ('completed', 'failed')
          AND updated_at < NOW() - (:days * INTERVAL '1 day')
        """,
        {"days": retention_days},
    )


async def _claim_due_job() -> dict | None:
    async with db.get_db().transaction():
        lease_token = uuid.uuid4().hex
        candidate = await db.fetch_one(
            """
            SELECT j.id
            FROM meta_inbound_jobs j
            WHERE j.status = 'pending'
              AND j.available_at <= NOW()
              AND NOT EXISTS (
                  SELECT 1 FROM meta_inbound_jobs active
                  WHERE active.channel = j.channel
                    AND active.sender_id = j.sender_id
                    AND active.status = 'processing'
              )
            ORDER BY j.available_at ASC, j.created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """
        )
        if not candidate:
            return None
        row = await db.fetch_one(
            """
            UPDATE meta_inbound_jobs
            SET status = 'processing', processing_started_at = NOW(),
                processing_heartbeat_at = NOW(), processing_lease_token = :lease_token,
                attempt_count = attempt_count + 1, updated_at = NOW()
            WHERE id = :job_id AND status = 'pending'
            RETURNING id, channel, sender_id, message_parts, media_url,
                      customer_profile, interaction_type, integration_context,
                      attempt_count, processing_lease_token
            """,
            {"job_id": str(candidate["id"]), "lease_token": lease_token},
        )
        return dict(row) if row else None


async def _process_claimed_job(job: dict) -> None:
    job_id = str(job["id"])
    lease_token = str(job.get("processing_lease_token") or "")
    heartbeat_task = asyncio.create_task(_heartbeat_claimed_job(job_id, lease_token))
    try:
        text = _combine_parts(_json_list(job["message_parts"]))
        if text:
            processor = _resolve_processor(job["channel"])
            integration_context = _json_dict(job.get("integration_context"))
            if job["channel"] == "instagram" and integration_context:
                from app.integrations.meta_context.service import enrich_native_instagram_context

                integration_context = await enrich_native_instagram_context(integration_context)
                await _store_enriched_integration_context(job_id, lease_token, integration_context)
            await processor(
                job["sender_id"],
                text,
                job.get("media_url"),
                _json_dict(job.get("customer_profile")),
                job_id,
                lease_token,
                job.get("interaction_type") or "private_message",
                integration_context,
            )
        completed = await db.fetch_one(
            """
            UPDATE meta_inbound_jobs
            SET status = 'completed', completed_at = NOW(),
                processing_started_at = NULL, processing_heartbeat_at = NULL,
                processing_lease_token = NULL, last_error = NULL, updated_at = NOW()
            WHERE id = :job_id AND status = 'processing' AND processing_lease_token = :lease_token
            RETURNING id
            """,
            {"job_id": job_id, "lease_token": lease_token},
        )
        if not completed:
            logger.warning("Skipped completion for stale Meta inbound lease job=%s", job_id)
    except Exception as exc:
        logger.exception("Durable inbound processing failed for job %s", job_id)
        await _requeue_failed_job(job, exc)
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task


async def _process_claimed_with_slot(job: dict) -> None:
    try:
        await _process_claimed_job(job)
    finally:
        _inbound_processor_semaphore.release()


async def _heartbeat_claimed_job(job_id: str, lease_token: str) -> None:
    if not lease_token:
        return
    try:
        while True:
            await asyncio.sleep(PROCESSING_HEARTBEAT_SECONDS)
            try:
                await db.execute(
                    """
                    UPDATE meta_inbound_jobs
                    SET processing_heartbeat_at = NOW(), updated_at = NOW()
                    WHERE id = :job_id AND status = 'processing' AND processing_lease_token = :lease_token
                    """,
                    {"job_id": job_id, "lease_token": lease_token},
                )
            except Exception:
                logger.exception("Failed to heartbeat Meta inbound job %s", job_id)
    except asyncio.CancelledError:
        raise


async def _requeue_failed_job(job: dict, error: Exception | str) -> None:
    """Retry a failed lease without colliding with a newer pending batch."""
    channel = job["channel"]
    sender_id = job["sender_id"]
    lease_token = str(job.get("processing_lease_token") or "")
    retry = int(job.get("attempt_count") or 0) < MAX_PROCESSING_ATTEMPTS
    known_safe_send_failure = isinstance(error, MetaSendError) and error.retryable
    error_name = type(error).__name__[:100] if isinstance(error, Exception) else str(error)[:100]
    async with db.get_db().transaction():
        await _lock_sender(channel, sender_id)
        current = await db.fetch_one(
            """
            SELECT id, message_parts, media_url, customer_profile,
                   interaction_type, integration_context,
                   outbound_started_at, outbound_message_ids
            FROM meta_inbound_jobs
            WHERE id = :job_id AND status = 'processing' AND processing_lease_token = :lease_token
            FOR UPDATE
            """,
            {"job_id": str(job["id"]), "lease_token": lease_token},
        )
        if not current:
            return
        outbound_ids = _json_list(_record_value(current, "outbound_message_ids", []))
        if outbound_ids:
            await db.execute(
                """
                UPDATE meta_inbound_jobs
                SET status = 'failed',
                    processing_started_at = NULL, processing_heartbeat_at = NULL,
                    processing_lease_token = NULL,
                    last_error = 'partial_delivery_requires_reconciliation',
                    updated_at = NOW()
                WHERE id = :job_id AND processing_lease_token = :lease_token
                """,
                {
                    "job_id": str(current["id"]),
                    "lease_token": lease_token,
                },
            )
            return
        if _record_value(current, "outbound_started_at") is not None and not known_safe_send_failure:
            await db.execute(
                """
                UPDATE meta_inbound_jobs
                SET status = 'failed', processing_started_at = NULL,
                    processing_heartbeat_at = NULL, processing_lease_token = NULL,
                    last_error = 'delivery_unknown_after_send_attempt', updated_at = NOW()
                WHERE id = :job_id AND processing_lease_token = :lease_token
                """,
                {"job_id": str(current["id"]), "lease_token": lease_token},
            )
            return
        current_context = _json_dict(_record_value(current, "integration_context", {}))
        pending = None
        if (
            _record_value(current, "interaction_type", "private_message") == "private_message"
            and not current_context
        ):
            pending = await db.fetch_one(
                """
                SELECT id, message_parts, media_url, customer_profile
                FROM meta_inbound_jobs
                WHERE channel = :channel AND sender_id = :sender_id
                  AND interaction_type = 'private_message'
                  AND integration_context = '{}'::jsonb
                  AND status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE
                """,
                {"channel": channel, "sender_id": sender_id},
            )
        if retry and pending:
            parts = _json_list(current["message_parts"]) + _json_list(pending["message_parts"])
            profile = _merge_profile(
                _json_dict(current["customer_profile"]),
                _json_dict(pending["customer_profile"]),
            )
            await db.execute(
                """
                UPDATE meta_inbound_jobs
                SET message_parts = CAST(:parts AS jsonb),
                    media_url = COALESCE(media_url, :failed_media_url),
                    customer_profile = CAST(:profile AS jsonb),
                    available_at = NOW() + INTERVAL '30 seconds', updated_at = NOW()
                WHERE id = :pending_id
                """,
                {
                    "parts": json.dumps(parts, ensure_ascii=False),
                    "failed_media_url": current["media_url"],
                    "profile": json.dumps(profile, ensure_ascii=False),
                    "pending_id": str(pending["id"]),
                },
            )
            await db.execute(
                """
                UPDATE meta_inbound_jobs
                SET status = 'failed', processing_started_at = NULL,
                    processing_heartbeat_at = NULL, processing_lease_token = NULL,
                    last_error = :error, updated_at = NOW()
                WHERE id = :job_id AND processing_lease_token = :lease_token
                """,
                {"error": error_name, "job_id": str(current["id"]), "lease_token": lease_token},
            )
            return

        await db.execute(
            """
            UPDATE meta_inbound_jobs
                SET status = :status,
                available_at = CASE WHEN :retry THEN NOW() + INTERVAL '30 seconds' ELSE available_at END,
                processing_started_at = NULL, processing_heartbeat_at = NULL,
                processing_lease_token = NULL, last_error = :error, updated_at = NOW()
            WHERE id = :job_id AND processing_lease_token = :lease_token
            """,
            {
                "status": "pending" if retry else "failed",
                "retry": retry,
                "error": error_name,
                "job_id": str(current["id"]),
                "lease_token": lease_token,
            },
        )


async def _process_after_delay(channel: str, sender_id: str) -> None:
    try:
        await asyncio.sleep(MESSAGE_DEBOUNCE_SECONDS)
        if not getattr(get_config(), "outbound_processing_enabled", True):
            return
        await _inbound_processor_semaphore.acquire()
        try:
            job = await _claim_sender_job(channel, sender_id)
        except BaseException:
            _inbound_processor_semaphore.release()
            raise
        if not job:
            _inbound_processor_semaphore.release()
            return
        await _process_claimed_with_slot(job)
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Inbound queue accelerator failed for %s/%s", channel, sender_id)


async def _claim_sender_job(channel: str, sender_id: str) -> dict | None:
    async with db.get_db().transaction():
        lease_token = uuid.uuid4().hex
        await _lock_sender(channel, sender_id)
        active = await db.fetch_one(
            """
            SELECT id FROM meta_inbound_jobs
            WHERE channel = :channel AND sender_id = :sender_id AND status = 'processing'
            """,
            {"channel": channel, "sender_id": sender_id},
        )
        if active:
            return None
        row = await db.fetch_one(
            """
            UPDATE meta_inbound_jobs
            SET status = 'processing', processing_started_at = NOW(),
                processing_heartbeat_at = NOW(), processing_lease_token = :lease_token,
                attempt_count = attempt_count + 1, updated_at = NOW()
            WHERE id = (
                SELECT id FROM meta_inbound_jobs
                WHERE channel = :channel AND sender_id = :sender_id
                  AND status = 'pending' AND available_at <= NOW()
                ORDER BY available_at ASC, created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, channel, sender_id, message_parts, media_url,
                      customer_profile, interaction_type, integration_context,
                      attempt_count, processing_lease_token
            """,
            {"channel": channel, "sender_id": sender_id, "lease_token": lease_token},
        )
        return dict(row) if row else None


async def _store_enriched_integration_context(
    job_id: str,
    lease_token: str,
    integration_context: dict,
) -> None:
    row = await db.fetch_one(
        """
        UPDATE meta_inbound_jobs
        SET integration_context = CAST(:integration_context AS jsonb), updated_at = NOW()
        WHERE id = :job_id
          AND status = 'processing'
          AND processing_lease_token = :lease_token
        RETURNING id
        """,
        {
            "job_id": job_id,
            "lease_token": lease_token,
            "integration_context": json.dumps(integration_context, ensure_ascii=False),
        },
    )
    if not row:
        raise RuntimeError("Meta inbound lease expired while enriching Instagram context.")


async def mark_outbound_send_started(job_id: str, lease_token: str) -> bool:
    """Mark that this lease entered the Meta send uncertainty window."""
    if not job_id or not lease_token:
        return False
    row = await db.fetch_one(
        """
        UPDATE meta_inbound_jobs
        SET outbound_started_at = COALESCE(outbound_started_at, NOW()),
            updated_at = NOW()
        WHERE id = :job_id AND status = 'processing' AND processing_lease_token = :lease_token
        RETURNING id
        """,
        {"job_id": job_id, "lease_token": lease_token},
    )
    return bool(row)


async def record_outbound_message(job_id: str, lease_token: str, response: dict | None) -> None:
    """Persist accepted Meta message IDs for stale-lease reconciliation."""
    message_ids = _extract_meta_message_ids(response or {})
    if not job_id or not lease_token or not message_ids:
        return
    await db.execute(
        """
        UPDATE meta_inbound_jobs
        SET outbound_message_ids = (
                SELECT jsonb_agg(DISTINCT value)
                FROM jsonb_array_elements_text(outbound_message_ids || CAST(:message_ids AS jsonb)) AS ids(value)
            ),
            updated_at = NOW()
        WHERE id = :job_id AND status = 'processing' AND processing_lease_token = :lease_token
        """,
        {
            "job_id": job_id,
            "lease_token": lease_token,
            "message_ids": json.dumps(message_ids),
        },
    )


async def send_with_delivery_record(send_func, inbound_job_id: str, lease_token: str, **kwargs):
    """Fence a Meta send and retry only failures known not to reach Meta."""
    if inbound_job_id and lease_token:
        marked = await mark_outbound_send_started(inbound_job_id, lease_token)
        if not marked:
            raise RuntimeError("Meta inbound lease is no longer active; refusing outbound send.")

    for attempt in range(MAX_META_SEND_ATTEMPTS):
        try:
            response = await send_func(**kwargs)
            break
        except MetaSendError as exc:
            if not exc.retryable or attempt + 1 >= MAX_META_SEND_ATTEMPTS:
                raise
            await asyncio.sleep(2**attempt)

    if inbound_job_id and lease_token:
        await record_outbound_message(inbound_job_id, lease_token, response)
    return response


def _extract_meta_message_ids(response: dict) -> list[str]:
    ids: list[str] = []
    for message in response.get("messages") or []:
        message_id = str((message or {}).get("id") or "").strip()
        if message_id:
            ids.append(message_id)
    direct_id = str(response.get("message_id") or response.get("id") or "").strip()
    if direct_id:
        ids.append(direct_id)
    return ids


async def _lock_sender(channel: str, sender_id: str) -> None:
    await db.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))",
        {"key": f"meta_inbound:{channel}:{sender_id}"},
    )


def _resolve_processor(channel: str) -> Processor:
    if channel == "whatsapp":
        from app.webhooks.whatsapp import _deliver_ai_response

        return _deliver_ai_response
    if channel == "instagram":
        from app.webhooks.instagram import _deliver_ai_response

        return _deliver_ai_response
    raise ValueError(f"Unsupported inbound channel: {channel}")


def _json_list(value) -> list:
    if isinstance(value, str):
        value = json.loads(value)
    return list(value or [])


def _json_dict(value) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value or {})


def _record_value(record, key: str, default=None):
    try:
        return record[key]
    except (KeyError, TypeError):
        return default
