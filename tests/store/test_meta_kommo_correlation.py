import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Handle:
    def transaction(self):
        return _Tx()


class _CorrelationDB:
    def __init__(self, events, jobs):
        self.events = {item["id"]: item for item in events}
        self.jobs = {item["id"]: item for item in jobs}

    def get_db(self):
        return _Handle()

    async def fetch_one(self, query, values=None):
        values = values or {}
        if "pg_advisory_xact_lock" in query:
            return {"locked": True}
        if "FROM meta_instagram_context_events" in query:
            event = self.events.get(values["id"])
            if not event or event["correlation_status"] not in {"pending", "ambiguous"}:
                return None
            return event
        if "FROM kommo_message_jobs" in query:
            job = self.jobs.get(values["id"])
            if not job or job["status"] != "waiting_for_context":
                return None
            return job
        if "UPDATE kommo_message_jobs" in query and "RETURNING id" in query:
            job = self.jobs[values["job_id"]]
            if job["status"] != "waiting_for_context" or job.get("meta_context_event_id"):
                return None
            job.update(
                context_status="matched",
                meta_context_event_id=values["event_id"],
                context_correlation_score=values["score"],
                public_comment_context=json.loads(values["context"]),
            )
            return {"id": job["id"]}
        return None

    async def fetch_all(self, query, values=None):
        if "FROM kommo_message_jobs" in query and "SELECT job.*" in query:
            return [
                job
                for job in self.jobs.values()
                if job["status"] == "waiting_for_context" and not job.get("meta_context_event_id")
            ]
        if "FROM meta_instagram_context_events" in query and "SELECT *" in query:
            return [
                event
                for event in self.events.values()
                if event["correlation_status"] in {"pending", "ambiguous"}
                and not event.get("matched_kommo_job_id")
            ]
        return []

    async def execute(self, query, values=None):
        values = values or {}
        if "UPDATE meta_instagram_context_events" in query:
            event = self.events[values.get("event_id") or values["id"]]
            if "correlation_status = 'matched'" in query:
                event.update(
                    correlation_status="matched",
                    matched_kommo_job_id=values["job_id"],
                )
            elif "correlation_status = 'ambiguous'" in query:
                event["correlation_status"] = "ambiguous"
        if "UPDATE kommo_message_jobs" in query and "context_status = 'ambiguous'" in query:
            self.jobs[values["id"]]["context_status"] = "ambiguous"
        return "UPDATE 1"


def _event(
    event_id="event-1",
    *,
    text="Precio?",
    username="cliente",
    seconds=0,
    comment_id="meta-comment",
    sender_id="meta-user-id",
):
    now = datetime.now(UTC)
    return {
        "id": event_id,
        "event_type": "comment",
        "correlation_status": "pending",
        "matched_kommo_job_id": None,
        "event_timestamp": now + timedelta(seconds=seconds),
        "expires_at": now + timedelta(minutes=1),
        "message_text": text,
        "sender_username": username,
        "sender_id": sender_id,
        "comment_id": comment_id,
        "parent_comment_id": "parent-1",
        "media_id": "media-1",
        "media_permalink": "https://www.instagram.com/p/ABC123/",
        "media_caption": "Caption",
    }


def _job(
    job_id="job-1",
    *,
    text="precio?",
    username="cliente",
    seconds=1,
    correlation_seconds=None,
    comment_id=None,
    author_id="kommo-author-id",
):
    now = datetime.now(UTC)
    created_at = now + timedelta(seconds=seconds)
    context = {"comment_id": comment_id} if comment_id else {}
    return {
        "id": job_id,
        "status": "waiting_for_context",
        "context_status": "pending",
        "meta_context_event_id": None,
        "interaction_type": "instagram_comment",
        "channel": "instagram",
        "created_at": created_at,
        "correlation_timestamp": now
        + timedelta(seconds=seconds if correlation_seconds is None else correlation_seconds),
        "correlation_timestamp_source": "salesbot_jwt_iat",
        "combined_message": text,
        "author_id": author_id,
        "author_username": username,
        "author_profile_url": None,
        "sender_username": None,
        "sender_profile_url": None,
        "public_comment_context": context,
    }


def _install(monkeypatch, events, jobs):
    from app.integrations.meta_context import correlation

    fake = _CorrelationDB(events, jobs)
    monkeypatch.setattr(correlation, "db", fake)
    monkeypatch.setattr(
        correlation,
        "get_config",
        lambda: SimpleNamespace(meta_context_match_window_seconds=45),
    )
    return correlation, fake


@pytest.mark.asyncio
async def test_meta_first_correlation_merges_context_and_releases_job(monkeypatch):
    event = _event()
    job = _job()
    correlation, fake = _install(monkeypatch, [event], [job])

    result = await correlation.correlate_meta_event("event-1")

    assert result["status"] == "matched"
    assert fake.events["event-1"]["matched_kommo_job_id"] == "job-1"
    matched_job = fake.jobs["job-1"]
    assert matched_job["status"] == "waiting_for_context"
    assert matched_job["context_status"] == "matched"
    assert matched_job["public_comment_context"] == {
        "comment_id": "meta-comment",
        "parent_comment_id": "parent-1",
        "media_id": "media-1",
        "post_id": "media-1",
        "post_url": "https://www.instagram.com/p/ABC123/",
        "post_caption": "Caption",
        "sender_username": "cliente",
        "context_provider": "meta",
        "correlation_status": "matched",
    }


@pytest.mark.asyncio
async def test_kommo_first_unique_username_text_timestamp_match(monkeypatch):
    event = _event()
    job = _job()
    correlation, _ = _install(monkeypatch, [event], [job])

    result = await correlation.correlate_kommo_job("job-1")

    assert result["status"] == "matched"
    assert result["score"] == 90


@pytest.mark.asyncio
async def test_ambiguous_identical_comments_are_never_auto_selected(monkeypatch):
    events = [_event("event-1"), _event("event-2")]
    job = _job()
    correlation, fake = _install(monkeypatch, events, [job])

    result = await correlation.correlate_kommo_job("job-1")

    assert result == {"status": "ambiguous", "candidate_count": 2, "score": 90}
    assert fake.jobs["job-1"]["status"] == "waiting_for_context"
    assert fake.jobs["job-1"]["context_status"] == "ambiguous"
    assert all(event["correlation_status"] == "ambiguous" for event in fake.events.values())


@pytest.mark.asyncio
async def test_event_anchored_correlation_requires_unique_match_for_job_too(monkeypatch):
    events = [_event("event-1"), _event("event-2")]
    job = _job()
    correlation, fake = _install(monkeypatch, events, [job])

    result = await correlation.correlate_meta_event("event-1")

    assert result == {"status": "ambiguous", "candidate_count": 2, "score": 90}
    assert fake.jobs["job-1"]["status"] == "waiting_for_context"
    assert fake.events["event-1"]["matched_kommo_job_id"] is None


@pytest.mark.asyncio
async def test_conflicting_known_usernames_reject_candidate(monkeypatch):
    event = _event(username="meta-user")
    job = _job(username="other-user")
    correlation, fake = _install(monkeypatch, [event], [job])

    result = await correlation.correlate_kommo_job("job-1")

    assert result == {"status": "pending"}
    assert fake.jobs["job-1"]["status"] == "waiting_for_context"


@pytest.mark.asyncio
async def test_media_id_without_permalink_can_correlate_while_job_waits(monkeypatch):
    event = _event()
    event["media_permalink"] = None
    job = _job()
    correlation, fake = _install(monkeypatch, [event], [job])

    result = await correlation.correlate_kommo_job("job-1")

    assert result["status"] == "matched"
    assert fake.jobs["job-1"]["status"] == "waiting_for_context"


@pytest.mark.asyncio
async def test_exact_comment_id_can_match_when_text_differs(monkeypatch):
    event = _event(text="Texto Meta", comment_id="shared-comment")
    job = _job(text="Texto Kommo", comment_id="shared-comment")
    correlation, _ = _install(monkeypatch, [event], [job])

    result = await correlation.correlate_meta_event("event-1")

    assert result["status"] == "matched"
    assert result["score"] == 160


@pytest.mark.asyncio
async def test_one_matching_username_among_multiple_identity_fields_is_accepted(monkeypatch):
    event = _event(username="matching-user")
    job = _job(username="different-author")
    job["sender_username"] = "matching-user"
    job["author_profile_url"] = "https://instagram.com/another-user/"
    correlation, _ = _install(monkeypatch, [event], [job])

    result = await correlation.correlate_kommo_job("job-1")

    assert result["status"] == "matched"
    assert result["score"] == 90


@pytest.mark.asyncio
async def test_different_meta_sender_and_kommo_author_ids_do_not_reject_match(monkeypatch):
    event = _event(sender_id="instagram-scoped-id")
    job = _job(author_id="kommo-contact-author-id")
    correlation, _ = _install(monkeypatch, [event], [job])

    result = await correlation.correlate_kommo_job("job-1")

    assert result["status"] == "matched"


@pytest.mark.asyncio
async def test_correlation_uses_best_timestamp_for_delayed_callback(monkeypatch):
    event = _event(seconds=0)
    job = _job(seconds=300, correlation_seconds=10)
    correlation, _ = _install(monkeypatch, [event], [job])

    result = await correlation.correlate_kommo_job("job-1")

    assert result["status"] == "matched"


@pytest.mark.asyncio
async def test_correlation_timestamp_still_enforces_configured_match_window(monkeypatch):
    event = _event(seconds=0)
    job = _job(seconds=1, correlation_seconds=46)
    correlation, fake = _install(monkeypatch, [event], [job])

    result = await correlation.correlate_kommo_job("job-1")

    assert result == {"status": "pending"}
    assert fake.jobs["job-1"]["status"] == "waiting_for_context"


def test_job_timestamp_helper_uses_documented_precedence():
    from app.integrations.meta_context import correlation

    fallback = datetime(2026, 7, 30, tzinfo=UTC)
    receipt = fallback + timedelta(seconds=1)
    jwt = fallback + timedelta(seconds=2)
    incoming = fallback + timedelta(seconds=3)
    job = {
        "created_at": fallback,
        "callback_receipt_timestamp": receipt,
        "callback_claims": {"iat": jwt.timestamp()},
        "incoming_message_timestamp": incoming,
    }

    assert correlation._job_correlation_timestamp(job) == incoming
    del job["incoming_message_timestamp"]
    assert correlation._job_correlation_timestamp(job) == jwt
    job["callback_claims"] = {"iat": "not-a-number"}
    assert correlation._job_correlation_timestamp(job) == receipt
    job["callback_claims"] = {"iat": 10**10000}
    assert correlation._job_correlation_timestamp(job) == receipt
    del job["callback_receipt_timestamp"]
    assert correlation._job_correlation_timestamp(job) == fallback


@pytest.mark.asyncio
async def test_candidate_job_sql_uses_receipt_jwt_and_created_at_precedence(monkeypatch):
    from app.integrations.meta_context import correlation

    mock_db = AsyncMock()
    mock_db.fetch_all = AsyncMock(return_value=[])
    monkeypatch.setattr(correlation, "db", mock_db)

    await correlation._candidate_jobs(_event(), 45)

    query = mock_db.fetch_all.await_args.args[0]
    assert "receipt.received_at" in query
    assert "callback_claims -> 'iat'" in query
    assert "receipt.created_at" in query
    assert "job.created_at" in query
    assert "AS correlation_timestamp" in query
    assert "<= :window_seconds" in query


@pytest.mark.asyncio
async def test_one_meta_event_cannot_match_two_kommo_jobs(monkeypatch):
    event = _event()
    jobs = [_job("job-1"), _job("job-2", seconds=10)]
    correlation, fake = _install(monkeypatch, [event], jobs)

    first = await correlation.correlate_kommo_job("job-1")
    second = await correlation.correlate_kommo_job("job-2")

    assert first["status"] == "matched"
    assert second["status"] == "pending"
    assert fake.jobs["job-2"]["meta_context_event_id"] is None


@pytest.mark.asyncio
async def test_context_deadline_releases_job_to_safe_ready_fallback(monkeypatch, caplog):
    from app.integrations.meta_context import correlation

    mock_db = AsyncMock()
    mock_db.fetch_all = AsyncMock(
        return_value=[
            {
                "id": "job-1",
                "context_status": "pending",
                "meta_context_event_id": None,
            }
        ]
    )
    mock_db.fetch_one = AsyncMock(return_value={"id": "job-1"})
    mock_db.execute = AsyncMock(return_value="UPDATE 0")
    monkeypatch.setattr(correlation, "db", mock_db)
    monkeypatch.setattr(
        correlation,
        "correlate_kommo_job",
        AsyncMock(return_value={"status": "pending"}),
    )

    with caplog.at_level("INFO", logger="app.integrations.meta_context.correlation"):
        result = await correlation.process_waiting_context_jobs(limit=10)

    assert result["timed_out"] == 1
    timeout_query = mock_db.fetch_one.await_args.args[0]
    assert "status = 'ready'" in timeout_query
    assert "context_status = 'timed_out'" in timeout_query
    assert "mapping_status', 'timed_out'" in timeout_query
    assert "WHERE id = :id" in timeout_query
    assert "meta_kommo_correlation_timed_out" in caplog.text


@pytest.mark.asyncio
async def test_mapping_gate_failure_cannot_block_safe_timeout(monkeypatch):
    from app.integrations.meta_context import correlation, service

    mock_db = AsyncMock()
    mock_db.fetch_all = AsyncMock(
        return_value=[
            {
                "id": "job-1",
                "context_status": "matched",
                "meta_context_event_id": "event-1",
            }
        ]
    )
    mock_db.fetch_one = AsyncMock(return_value={"id": "job-1"})
    mock_db.execute = AsyncMock(return_value="UPDATE 0")
    monkeypatch.setattr(correlation, "db", mock_db)
    monkeypatch.setattr(
        service,
        "resolve_and_release_matched_job",
        AsyncMock(side_effect=RuntimeError("mapping database unavailable")),
    )

    result = await correlation.process_waiting_context_jobs(limit=10)

    assert result["timed_out"] == 1
    assert "mapping_status', 'timed_out'" in mock_db.fetch_one.await_args.args[0]
