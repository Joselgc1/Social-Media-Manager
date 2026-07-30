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
        SELECT id
        FROM kommo_message_jobs
        WHERE status = 'waiting_for_context'
        ORDER BY context_deadline_at ASC, created_at ASC
        LIMIT :limit
        """,
        {"limit": limit},
    )
    matched = 0
    for row in rows:
        result = await correlate_kommo_job(str(row["id"]))
        matched += int(result.get("status") == "matched")

    timed_out_rows = await db.fetch_all(
        """
        UPDATE kommo_message_jobs
        SET status = 'ready',
            context_status = 'timed_out',
            last_error = 'Meta Instagram context deadline elapsed',
            updated_at = NOW()
        WHERE status = 'waiting_for_context'
          AND context_deadline_at <= NOW()
        RETURNING id
        """
    )
    for row in timed_out_rows:
        logger.info("meta_kommo_correlation_timed_out job_id=%s", row["id"])

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
        "timed_out": len(timed_out_rows),
        "expired": _affected_rows(expired),
    }


async def schedule_context_job_processing(job_id: str) -> None:
    """Accelerate correlation while PostgreSQL and the scheduler remain authoritative."""
    result = await correlate_kommo_job(job_id)
    if result.get("status") != "matched":
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
          AND (media_id IS NULL OR media_permalink IS NOT NULL)
        FOR UPDATE
        """,
        {"id": event_id},
    )


async def _load_job(job_id: str):
    return await db.fetch_one(
        """
        SELECT *
        FROM kommo_message_jobs
        WHERE id = :id
          AND status = 'waiting_for_context'
          AND interaction_type = 'instagram_comment'
          AND channel = 'instagram'
          AND context_status IN ('pending', 'ambiguous')
          AND meta_context_event_id IS NULL
        FOR UPDATE
        """,
        {"id": job_id},
    )


async def _candidate_jobs(event: dict, window_seconds: int):
    return await db.fetch_all(
        """
        SELECT *
        FROM kommo_message_jobs
        WHERE status = 'waiting_for_context'
          AND interaction_type = 'instagram_comment'
          AND channel = 'instagram'
          AND context_status IN ('pending', 'ambiguous')
          AND meta_context_event_id IS NULL
          AND ABS(EXTRACT(EPOCH FROM (created_at - :event_timestamp))) <= :window_seconds
        ORDER BY created_at ASC, id ASC
        FOR UPDATE
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
          AND (media_id IS NULL OR media_permalink IS NOT NULL)
          AND ABS(EXTRACT(EPOCH FROM (event_timestamp - :job_timestamp))) <= :window_seconds
        ORDER BY event_timestamp ASC, id ASC
        FOR UPDATE
        """,
        {"job_timestamp": job["created_at"], "window_seconds": window_seconds},
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
    if event.get("media_id") and not event.get("media_permalink"):
        return None
    difference = abs((_as_datetime(event["event_timestamp"]) - _as_datetime(job["created_at"])).total_seconds())
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
    if event_username and job_usernames and job_usernames != {event_username}:
        return None
    event_sender_id = str(event.get("sender_id") or "").strip()
    job_sender_id = str(job.get("author_id") or "").strip()
    if event_sender_id and job_sender_id and event_sender_id != job_sender_id:
        return None

    score = 0
    signals = []
    if exact_comment_id:
        score += 100
        signals.append("comment_id")
    if event_username and event_username in job_usernames:
        score += 40
        signals.append("username")
    if event_sender_id and event_sender_id == job_sender_id:
        score += 50
        signals.append("sender_id")
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
    }
    job = await db.fetch_one(
        """
        UPDATE kommo_message_jobs
        SET meta_context_event_id = :event_id,
            context_status = 'matched',
            context_correlation_score = :score,
            public_comment_context = COALESCE(public_comment_context, '{}'::jsonb) || CAST(:context AS jsonb),
            status = 'ready',
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


def _affected_rows(value) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.rsplit(" ", 1)[-1])
        except ValueError:
            return 0
    return 0
