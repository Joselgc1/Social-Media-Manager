"""Durable debounce queue for Meta inbound messages."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from app import db

logger = logging.getLogger(__name__)

MESSAGE_DEBOUNCE_SECONDS = 10
PROCESSING_LEASE_MINUTES = 5
MAX_PROCESSING_ATTEMPTS = 5

Processor = Callable[[str, str, str | None, dict, str], Awaitable[None]]


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
) -> bool:
    """Persist one Meta delivery and extend its customer's debounce window."""
    if channel not in {"whatsapp", "instagram"}:
        raise ValueError("Unsupported inbound channel.")
    if not sender_id or not message_id:
        raise ValueError("Inbound messages require sender_id and message_id.")

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

        pending = await db.fetch_one(
            """
            SELECT id, message_parts, customer_profile
            FROM meta_inbound_jobs
            WHERE channel = :channel
              AND sender_id = :sender_id
              AND status = 'pending'
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
                    customer_profile, available_at
                )
                VALUES (
                    :channel, :sender_id, CAST(:parts AS jsonb), :media_url,
                    CAST(:profile AS jsonb), NOW() + (:delay * INTERVAL '1 second')
                )
                RETURNING id
                """,
                {
                    "channel": channel,
                    "sender_id": sender_id,
                    "parts": json.dumps([text], ensure_ascii=False),
                    "media_url": media_url,
                    "profile": json.dumps(customer_profile or {}, ensure_ascii=False),
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

    asyncio.create_task(_process_after_delay(channel, sender_id))
    return True


async def process_due_inbound_jobs(limit: int = 10) -> int:
    """Lease and process due batches. Safe to call concurrently."""
    await recover_stale_inbound_jobs()
    processed = 0
    for _ in range(limit):
        job = await _claim_due_job()
        if not job:
            break
        await _process_claimed_job(job)
        processed += 1
    return processed


async def recover_stale_inbound_jobs() -> int:
    """Return abandoned processing leases to pending without losing newer messages."""
    stale_rows = await db.fetch_all(
        """
        SELECT id, channel, sender_id
        FROM meta_inbound_jobs
        WHERE status = 'processing'
          AND processing_started_at < NOW() - (:minutes * INTERVAL '1 minute')
        ORDER BY processing_started_at ASC
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
                SELECT id, message_parts, media_url, customer_profile, attempt_count
                FROM meta_inbound_jobs
                WHERE id = :job_id AND status = 'processing'
                FOR UPDATE
                """,
                {"job_id": str(stale["id"])},
            )
            if not stale_job:
                continue
            pending = await db.fetch_one(
                """
                SELECT id, message_parts, media_url, customer_profile
                FROM meta_inbound_jobs
                WHERE channel = :channel AND sender_id = :sender_id AND status = 'pending'
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
                    SET status = 'failed', last_error = 'stale_lease_merged', updated_at = NOW()
                    WHERE id = :job_id
                    """,
                    {"job_id": str(stale_job["id"])},
                )
            else:
                await db.execute(
                    """
                    UPDATE meta_inbound_jobs
                    SET status = 'pending', available_at = NOW(),
                        processing_started_at = NULL, last_error = 'stale_lease_recovered',
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
                attempt_count = attempt_count + 1, updated_at = NOW()
            WHERE id = :job_id AND status = 'pending'
            RETURNING id, channel, sender_id, message_parts, media_url,
                      customer_profile, attempt_count
            """,
            {"job_id": str(candidate["id"])},
        )
        return dict(row) if row else None


async def _process_claimed_job(job: dict) -> None:
    job_id = str(job["id"])
    try:
        text = _combine_parts(_json_list(job["message_parts"]))
        if text:
            processor = _resolve_processor(job["channel"])
            await processor(
                job["sender_id"],
                text,
                job.get("media_url"),
                _json_dict(job.get("customer_profile")),
                job_id,
            )
        await db.execute(
            """
            UPDATE meta_inbound_jobs
            SET status = 'completed', completed_at = NOW(),
                processing_started_at = NULL, last_error = NULL, updated_at = NOW()
            WHERE id = :job_id AND status = 'processing'
            """,
            {"job_id": job_id},
        )
    except Exception as exc:
        logger.exception("Durable inbound processing failed for job %s", job_id)
        await _requeue_failed_job(job, type(exc).__name__[:100])


async def _requeue_failed_job(job: dict, error: str) -> None:
    """Retry a failed lease without colliding with a newer pending batch."""
    channel = job["channel"]
    sender_id = job["sender_id"]
    retry = int(job.get("attempt_count") or 0) < MAX_PROCESSING_ATTEMPTS
    async with db.get_db().transaction():
        await _lock_sender(channel, sender_id)
        current = await db.fetch_one(
            """
            SELECT id, message_parts, media_url, customer_profile
            FROM meta_inbound_jobs
            WHERE id = :job_id AND status = 'processing'
            FOR UPDATE
            """,
            {"job_id": str(job["id"])},
        )
        if not current:
            return
        pending = await db.fetch_one(
            """
            SELECT id, message_parts, media_url, customer_profile
            FROM meta_inbound_jobs
            WHERE channel = :channel AND sender_id = :sender_id AND status = 'pending'
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
                    last_error = :error, updated_at = NOW()
                WHERE id = :job_id
                """,
                {"error": error, "job_id": str(current["id"])},
            )
            return

        await db.execute(
            """
            UPDATE meta_inbound_jobs
            SET status = :status,
                available_at = CASE WHEN :retry THEN NOW() + INTERVAL '30 seconds' ELSE available_at END,
                processing_started_at = NULL, last_error = :error, updated_at = NOW()
            WHERE id = :job_id
            """,
            {
                "status": "pending" if retry else "failed",
                "retry": retry,
                "error": error,
                "job_id": str(current["id"]),
            },
        )


async def _process_after_delay(channel: str, sender_id: str) -> None:
    try:
        await asyncio.sleep(MESSAGE_DEBOUNCE_SECONDS)
        job = await _claim_sender_job(channel, sender_id)
        if job:
            await _process_claimed_job(job)
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Inbound queue accelerator failed for %s/%s", channel, sender_id)


async def _claim_sender_job(channel: str, sender_id: str) -> dict | None:
    async with db.get_db().transaction():
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
                      customer_profile, attempt_count
            """,
            {"channel": channel, "sender_id": sender_id},
        )
        return dict(row) if row else None


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
