"""Deterministic one-to-one correlation between Meta comments and Kommo jobs."""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from app import db
from app.config import get_config
from app.integrations.meta_context.normalization import (
    normalize_username,
    normalized_text_hash,
    username_from_profile_url,
)

logger = logging.getLogger(__name__)

_RECEIPT_MATCH_SQL = """
receipt.job_id = job.id
AND receipt.external_message_id = job.external_message_id
"""
_VALID_JWT_IAT_SQL = """
CASE
    WHEN jsonb_typeof(job.callback_claims -> 'iat') = 'number'
    THEN CAST(job.callback_claims ->> 'iat' AS numeric) BETWEEN 0 AND 32503680000
    ELSE FALSE
END
"""
_JOB_CORRELATION_TIMESTAMP_SQL = f"""
COALESCE(
    (
        SELECT receipt.received_at
        FROM kommo_message_receipts receipt
        WHERE {_RECEIPT_MATCH_SQL}
          AND receipt.received_at IS NOT NULL
        ORDER BY receipt.created_at DESC
        LIMIT 1
    ),
    CASE
        WHEN {_VALID_JWT_IAT_SQL}
        THEN to_timestamp(CAST(job.callback_claims ->> 'iat' AS double precision))
    END,
    (
        SELECT receipt.created_at
        FROM kommo_message_receipts receipt
        WHERE {_RECEIPT_MATCH_SQL}
        ORDER BY receipt.created_at DESC
        LIMIT 1
    ),
    job.created_at
)
"""
_JOB_CORRELATION_TIMESTAMP_SOURCE_SQL = f"""
CASE
    WHEN EXISTS (
        SELECT 1 FROM kommo_message_receipts receipt
        WHERE {_RECEIPT_MATCH_SQL}
          AND receipt.received_at IS NOT NULL
    ) THEN 'incoming_message_timestamp'
    WHEN {_VALID_JWT_IAT_SQL} THEN 'salesbot_jwt_iat'
    WHEN EXISTS (
        SELECT 1 FROM kommo_message_receipts receipt
        WHERE {_RECEIPT_MATCH_SQL}
    ) THEN 'callback_receipt_timestamp'
    ELSE 'job_created_at'
END
"""


@dataclass(frozen=True)
class Candidate:
    event: dict
    job: dict
    score: int
    timestamp_difference_seconds: float
    signals: tuple[str, ...]


async def correlate_meta_event(event_id: str) -> dict:
    return await _correlate(meta_event_id=event_id)


async def correlate_kommo_job(job_id: str) -> dict:
    return await _correlate(kommo_job_id=job_id)


async def _correlate(
    *, meta_event_id: str | None = None,
    kommo_job_id: str | None = None,
) -> dict:
    if not meta_event_id and not kommo_job_id:
        raise ValueError("A Meta event or Kommo job ID is required")
    config = get_config()
    async with db.get_db().transaction():
        await db.fetch_one(
            "SELECT pg_advisory_xact_lock(hashtext('meta-instagram-kommo-correlation'))"
        )
        if meta_event_id:
            event = await _load_event(meta_event_id)
            if not event or event["correlation_status"] == "matched":
                return {"status": "already_matched" if event else "missing"}
            jobs = await _candidate_jobs(event, config.meta_context_match_window_seconds)
            candidates = [_score_candidate(dict(event), dict(job), config.meta_context_match_window_seconds) for job in jobs]
            candidates = [item for item in candidates if item]
            anchor_kind = "event"
        else:
            job = await _load_job(kommo_job_id)
            if not job or job["context_status"] == "matched":
                return {"status": "already_matched" if job else "missing"}
            events = await _candidate_events(job, config.meta_context_match_window_seconds)
            candidates = [_score_candidate(dict(event), dict(job), config.meta_context_match_window_seconds) for event in events]
            candidates = [item for item in candidates if item]
            anchor_kind = "job"

        if not candidates:
            logger.info(
                "meta_kommo_correlation_pending anchor_type=%s reason=no_unique_candidate",
                anchor_kind,
            )
            return {"status": "pending"}
        best_score = max(item.score for item in candidates)
        winners = [item for item in candidates if item.score == best_score]
        if len(winners) != 1:
            await _mark_ambiguous(winners, anchor_kind)
            logger.info(
                "meta_kommo_correlation_ambiguous anchor_type=%s candidate_count=%s score=%s",
                anchor_kind,
                len(winners),
                best_score,
            )
            return {"status": "ambiguous", "candidate_count": len(winners), "score": best_score}

        winner = winners[0]
        opposite = await _opposite_candidates(
            winner,
            anchor_kind=anchor_kind,
            window_seconds=config.meta_context_match_window_seconds,
        )
        if not opposite:
            logger.info(
                "meta_kommo_correlation_pending anchor_type=%s reason=no_reciprocal_candidate",
                anchor_kind,
            )
            return {"status": "pending"}
        opposite_best_score = max(item.score for item in opposite)
        opposite_winners = [item for item in opposite if item.score == opposite_best_score]
        if len(opposite_winners) != 1:
            await _mark_ambiguous(opposite_winners, "opposite")
            logger.info(
                "meta_kommo_correlation_ambiguous anchor_type=%s candidate_count=%s score=%s",
                "opposite",
                len(opposite_winners),
                opposite_best_score,
            )
            return {
                "status": "ambiguous",
                "candidate_count": len(opposite_winners),
                "score": opposite_best_score,
            }
        if not _same_pair(winner, opposite_winners[0]):
            logger.info(
                "meta_kommo_correlation_pending anchor_type=%s reason=reciprocal_pair_mismatch",
                anchor_kind,
            )
            return {"status": "pending"}
        matched = await _persist_match(winner)
        if not matched:
            return {"status": "pending"}

    logger.info(
        "meta_kommo_correlation_matched event_id=%s job_id=%s score=%s signals=%s",
        winner.event["id"],
        winner.job["id"],
        winner.score,
        list(winner.signals),
    )
    return {
        "status": "matched",
        "event_id": str(winner.event["id"]),
        "job_id": str(winner.job["id"]),
        "score": winner.score,
    }


async def process_waiting_context_jobs(limit: int = 10) -> dict:
    """Make one last correlation attempt, then release due jobs to safe fallback."""
    rows = await db.fetch_all(
        """
        SELECT id, context_status, meta_context_event_id
        FROM kommo_message_jobs
        WHERE status = 'waiting_for_context'
        ORDER BY context_deadline_at ASC, created_at ASC
        LIMIT :limit
        """,
        {"limit": limit},
    )
    matched = 0
    timed_out = 0
    for row in rows:
        job_id = str(row["id"])
        event_id = str(row["meta_context_event_id"]) if row["meta_context_event_id"] else None
        try:
            newly_matched = False
            if row["context_status"] != "matched":
                result = await correlate_kommo_job(job_id)
                newly_matched = result.get("status") == "matched"
                matched += int(newly_matched)
                event_id = result.get("event_id") or event_id
            if event_id:
                from app.integrations.meta_context.service import resolve_and_release_matched_job

                await resolve_and_release_matched_job(event_id, force=newly_matched)
        except Exception:
            logger.exception("Meta context mapping gate failed: job_id=%s", job_id)
        timed_out += int(await _release_context_job_if_due(job_id))

    expired = await db.execute(
        """
        UPDATE meta_instagram_context_events
        SET correlation_status = CASE
                WHEN correlation_status = 'matched' THEN 'matched'
                ELSE 'expired'
            END,
            message_text = NULL,
            sender_id = NULL,
            sender_username = NULL,
            updated_at = NOW()
        WHERE correlation_status IN ('pending', 'ambiguous', 'matched')
          AND expires_at <= NOW()
          AND (message_text IS NOT NULL OR sender_id IS NOT NULL OR sender_username IS NOT NULL)
        """
    )
    return {
        "checked": len(rows),
        "matched": matched,
        "timed_out": timed_out,
        "expired": _affected_rows(expired),
    }


async def release_timed_out_context_jobs(limit: int = 10) -> int:
    """Release old waiting jobs without running Meta work after the feature is disabled."""
    rows = await db.fetch_all(
        """
        SELECT id
        FROM kommo_message_jobs
        WHERE status = 'waiting_for_context'
          AND context_deadline_at <= NOW()
        ORDER BY context_deadline_at ASC, created_at ASC
        LIMIT :limit
        """,
        {"limit": limit},
    )
    released = 0
    for row in rows:
        released += int(await _release_context_job_if_due(str(row["id"])))
    return released


async def _release_context_job_if_due(job_id: str) -> bool:
    row = await db.fetch_one(
        """
        UPDATE kommo_message_jobs
        SET status = 'ready',
            context_status = 'timed_out',
            public_comment_context = COALESCE(public_comment_context, '{}'::jsonb)
                || jsonb_build_object('mapping_status', 'timed_out'),
            last_error = 'Meta Instagram context deadline elapsed',
            updated_at = NOW()
        WHERE id = :id
          AND status = 'waiting_for_context'
          AND context_deadline_at <= NOW()
        RETURNING id
        """,
        {"id": job_id},
    )
    if not row:
        return False
    logger.info("meta_kommo_correlation_timed_out job_id=%s", row["id"])
    return True


async def schedule_context_job_processing(job_id: str) -> None:
    """Accelerate correlation while PostgreSQL and the scheduler remain authoritative."""
    try:
        result = await correlate_kommo_job(job_id)
    except Exception:
        logger.exception("Meta context correlation accelerator failed: job_id=%s", job_id)
        result = {"status": "pending"}
    release_result = {"status": "waiting"}
    if result.get("status") == "matched":
        from app.integrations.meta_context.service import resolve_and_release_matched_job

        try:
            release_result = await resolve_and_release_matched_job(
                result["event_id"],
                force=True,
            )
        except Exception:
            logger.exception("Meta context mapping gate failed: job_id=%s", job_id)
    if release_result.get("status") != "ready":
        await asyncio.sleep(get_config().meta_context_wait_seconds + 0.2)
        await process_waiting_context_jobs(limit=10)
    from app.integrations.kommo.jobs import process_ready_jobs

    await process_ready_jobs(limit=3)


async def _load_event(event_id: str):
    return await db.fetch_one(
        """
        SELECT *
        FROM meta_instagram_context_events
        WHERE id = :id
          AND event_type = 'comment'
          AND correlation_status IN ('pending', 'ambiguous')
          AND matched_kommo_job_id IS NULL
          AND expires_at > NOW()
        FOR UPDATE
        """,
        {"id": event_id},
    )


async def _load_job(job_id: str):
    return await db.fetch_one(
        f"""
        SELECT job.*,
               {_JOB_CORRELATION_TIMESTAMP_SQL} AS correlation_timestamp,
               {_JOB_CORRELATION_TIMESTAMP_SOURCE_SQL} AS correlation_timestamp_source
        FROM kommo_message_jobs job
        WHERE job.id = :id
          AND job.status = 'waiting_for_context'
          AND job.interaction_type = 'instagram_comment'
          AND job.channel = 'instagram'
          AND job.context_status IN ('pending', 'ambiguous')
          AND job.meta_context_event_id IS NULL
        FOR UPDATE OF job
        """,
        {"id": job_id},
    )


async def _candidate_jobs(event: dict, window_seconds: int):
    return await db.fetch_all(
        f"""
        SELECT job.*,
               {_JOB_CORRELATION_TIMESTAMP_SQL} AS correlation_timestamp,
               {_JOB_CORRELATION_TIMESTAMP_SOURCE_SQL} AS correlation_timestamp_source
        FROM kommo_message_jobs job
        WHERE job.status = 'waiting_for_context'
          AND job.interaction_type = 'instagram_comment'
          AND job.channel = 'instagram'
          AND job.context_status IN ('pending', 'ambiguous')
          AND job.meta_context_event_id IS NULL
          AND ABS(EXTRACT(EPOCH FROM (
              {_JOB_CORRELATION_TIMESTAMP_SQL} - :event_timestamp
          ))) <= :window_seconds
        ORDER BY job.created_at ASC, job.id ASC
        FOR UPDATE OF job
        """,
        {"event_timestamp": event["event_timestamp"], "window_seconds": window_seconds},
    )


async def _candidate_events(job: dict, window_seconds: int):
    return await db.fetch_all(
        """
        SELECT *
        FROM meta_instagram_context_events
        WHERE event_type = 'comment'
          AND correlation_status IN ('pending', 'ambiguous')
          AND matched_kommo_job_id IS NULL
          AND expires_at > NOW()
          AND ABS(EXTRACT(EPOCH FROM (event_timestamp - :job_timestamp))) <= :window_seconds
        ORDER BY event_timestamp ASC, id ASC
        FOR UPDATE
        """,
        {
            "job_timestamp": _job_correlation_timestamp(dict(job)),
            "window_seconds": window_seconds,
        },
    )


async def _opposite_candidates(
    candidate: Candidate,
    *,
    anchor_kind: str,
    window_seconds: int,
) -> list[Candidate]:
    if anchor_kind == "event":
        rows = await _candidate_events(candidate.job, window_seconds)
        candidates = [
            _score_candidate(dict(event), candidate.job, window_seconds)
            for event in rows
        ]
    else:
        rows = await _candidate_jobs(candidate.event, window_seconds)
        candidates = [
            _score_candidate(candidate.event, dict(job), window_seconds)
            for job in rows
        ]
    return [item for item in candidates if item]


def _same_pair(left: Candidate, right: Candidate) -> bool:
    return (
        str(left.event["id"]) == str(right.event["id"])
        and str(left.job["id"]) == str(right.job["id"])
    )


def _score_candidate(event: dict, job: dict, window_seconds: int) -> Candidate | None:
    difference = abs(
        (
            _as_datetime(event["event_timestamp"])
            - _job_correlation_timestamp(job)
        ).total_seconds()
    )
    if difference > window_seconds:
        return None
    context = _job_context(job)
    exact_comment_id = bool(
        event.get("comment_id")
        and context.get("comment_id")
        and str(event["comment_id"]) == str(context["comment_id"])
    )
    exact_text = normalized_text_hash(event.get("message_text")) == normalized_text_hash(
        job.get("combined_message")
    )
    if not exact_comment_id and not exact_text:
        return None

    event_username = normalize_username(event.get("sender_username"))
    job_usernames = _job_usernames(job)
    if event_username and job_usernames and event_username not in job_usernames:
        return None

    score = 0
    signals = []
    if exact_comment_id:
        score += 100
        signals.append("comment_id")
    if event_username and event_username in job_usernames:
        score += 40
        signals.append("username")
    if exact_text:
        score += 30
        signals.append("text")
    if difference <= 5:
        score += 20
        signals.append("time_5s")
    elif difference <= 15:
        score += 10
        signals.append("time_15s")
    else:
        score += 5
        signals.append("time_window")
    return Candidate(event, job, score, difference, tuple(signals))


def _job_usernames(job: dict) -> set[str]:
    values = {
        normalize_username(job.get("author_username")),
        normalize_username(job.get("sender_username")),
        username_from_profile_url(job.get("author_profile_url")),
        username_from_profile_url(job.get("sender_profile_url")),
    }
    return {value for value in values if value}


async def _mark_ambiguous(candidates: list[Candidate], anchor_kind: str) -> None:
    event_ids = {str(item.event["id"]) for item in candidates}
    job_ids = {str(item.job["id"]) for item in candidates}
    details = json.dumps({"reason": "equal_best_candidates", "candidate_count": len(candidates)})
    for event_id in event_ids:
        await db.execute(
            """
            UPDATE meta_instagram_context_events
            SET correlation_status = 'ambiguous',
                correlation_details = correlation_details || CAST(:details AS jsonb),
                updated_at = NOW()
            WHERE id = :id AND matched_kommo_job_id IS NULL
            """,
            {"id": event_id, "details": details},
        )
    for job_id in job_ids:
        await db.execute(
            """
            UPDATE kommo_message_jobs
            SET context_status = 'ambiguous', updated_at = NOW()
            WHERE id = :id AND meta_context_event_id IS NULL
            """,
            {"id": job_id},
        )


async def _persist_match(candidate: Candidate) -> bool:
    context = {
        "comment_id": candidate.event.get("comment_id"),
        "parent_comment_id": candidate.event.get("parent_comment_id"),
        "media_id": candidate.event.get("media_id"),
        "post_id": candidate.event.get("media_id"),
        "post_url": candidate.event.get("media_permalink"),
        "post_caption": candidate.event.get("media_caption"),
        "sender_username": candidate.event.get("sender_username"),
        "context_provider": "meta",
        "correlation_status": "matched",
    }
    context = {key: value for key, value in context.items() if value is not None}
    details = {
        "score": candidate.score,
        "signals": list(candidate.signals),
        "timestamp_difference_seconds": candidate.timestamp_difference_seconds,
        "job_timestamp_source": _job_correlation_timestamp_source(candidate.job),
        "job_correlation_timestamp": _job_correlation_timestamp(candidate.job).isoformat(),
    }
    job = await db.fetch_one(
        """
        UPDATE kommo_message_jobs
        SET meta_context_event_id = :event_id,
            context_status = 'matched',
            context_correlation_score = :score,
            public_comment_context = COALESCE(public_comment_context, '{}'::jsonb) || CAST(:context AS jsonb),
            last_error = NULL,
            updated_at = NOW()
        WHERE id = :job_id
          AND status = 'waiting_for_context'
          AND meta_context_event_id IS NULL
        RETURNING id
        """,
        {
            "event_id": candidate.event["id"],
            "job_id": candidate.job["id"],
            "score": candidate.score,
            "context": json.dumps(context, ensure_ascii=False),
        },
    )
    if not job:
        return False
    await db.execute(
        """
        UPDATE meta_instagram_context_events
        SET correlation_status = 'matched',
            matched_kommo_job_id = :job_id,
            correlation_details = correlation_details || CAST(:details AS jsonb),
            updated_at = NOW()
        WHERE id = :event_id
          AND matched_kommo_job_id IS NULL
        """,
        {
            "event_id": candidate.event["id"],
            "job_id": candidate.job["id"],
            "details": json.dumps(details),
        },
    )
    return True


def _job_context(job: dict) -> dict:
    value = job.get("public_comment_context")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_datetime(value) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _job_correlation_timestamp(job: dict) -> datetime:
    incoming_timestamp = job.get("incoming_message_timestamp")
    if incoming_timestamp:
        return _as_datetime(incoming_timestamp)
    if job.get("correlation_timestamp"):
        return _as_datetime(job["correlation_timestamp"])
    claims = job.get("callback_claims")
    if isinstance(claims, str):
        try:
            claims = json.loads(claims)
        except json.JSONDecodeError:
            claims = {}
    issued_at = _jwt_iat_datetime(claims)
    if issued_at:
        return issued_at
    if job.get("callback_receipt_timestamp"):
        return _as_datetime(job["callback_receipt_timestamp"])
    return _as_datetime(job["created_at"])


def _job_correlation_timestamp_source(job: dict) -> str:
    if job.get("correlation_timestamp_source"):
        return str(job["correlation_timestamp_source"])
    if job.get("incoming_message_timestamp"):
        return "incoming_message_timestamp"
    claims = job.get("callback_claims")
    if isinstance(claims, str):
        try:
            claims = json.loads(claims)
        except json.JSONDecodeError:
            claims = {}
    if _jwt_iat_datetime(claims):
        return "salesbot_jwt_iat"
    if job.get("callback_receipt_timestamp"):
        return "callback_receipt_timestamp"
    return "job_created_at"


def _jwt_iat_datetime(claims) -> datetime | None:
    issued_at = claims.get("iat") if isinstance(claims, dict) else None
    if not isinstance(issued_at, (int, float)) or isinstance(issued_at, bool):
        return None
    try:
        numeric_issued_at = float(issued_at)
    except (OSError, OverflowError, ValueError):
        return None
    if not 0 <= numeric_issued_at <= 32_503_680_000:
        return None
    try:
        return datetime.fromtimestamp(numeric_issued_at, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _affected_rows(value) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.rsplit(" ", 1)[-1])
        except ValueError:
            return 0
    return 0
