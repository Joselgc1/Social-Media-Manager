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
        if "FROM meta_instagram_context_events" in query and "SELECT event.*" in query:
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


def test_verified_customer_identity_is_exact_high_score_signal_despite_username_difference():
    from app.integrations.meta_context import correlation

    event = _event(username="meta-name", sender_id="meta-scoped-id")
    job = _job(username="kommo-name", author_id="unrelated-kommo-author")
    job["_event_customer_id"] = "customer-1"
    job["_job_customer_id"] = "customer-1"

    candidate = correlation._score_candidate(event, job, 45)

    assert candidate is not None
    assert candidate.score == 170
    assert "customer_identity" in candidate.signals
    assert "username" not in candidate.signals


def test_verified_customer_identity_mismatch_rejects_candidate():
    from app.integrations.meta_context import correlation

    event = _event(sender_id="meta-scoped-id")
    job = _job(author_id="kommo-author-id")
    job["_event_customer_id"] = "customer-1"
    job["_job_customer_id"] = "customer-2"

    assert correlation._score_candidate(event, job, 45) is None


def test_raw_meta_sender_and_kommo_author_ids_are_not_compared_as_customer_identity():
    from app.integrations.meta_context import correlation

    event = _event(sender_id="same-looking-id")
    job = _job(author_id="different-id")
    candidate = correlation._score_candidate(event, job, 45)

    assert candidate is not None
    assert "customer_identity" not in candidate.signals


def test_comment_text_and_time_without_identity_or_content_never_correlates():
    from app.integrations.meta_context import correlation

    event = _event(username="meta-user", comment_id="meta-comment")
    job = _job(username=None, comment_id=None)

    assert correlation._score_candidate(event, job, 45) is None


def test_comment_matching_media_identity_can_correlate_without_username():
    from app.integrations.meta_context import correlation

    event = _event(username=None, comment_id=None)
    job = _job(username=None, comment_id=None)
    job["public_comment_context"] = {"media_id": "media-1"}

    candidate = correlation._score_candidate(event, job, 45)

    assert candidate is not None
    assert "content_identity" in candidate.signals


@pytest.mark.asyncio
async def test_cross_post_same_text_callback_stays_pending_without_strong_signal(monkeypatch):
    event = _event(username="meta-user", comment_id="event-comment")
    event["media_id"] = "post-a"
    event["media_permalink"] = "https://www.instagram.com/p/POST_A/"
    job = _job(username=None, comment_id=None)
    job["public_comment_context"] = {}
    correlation, fake = _install(monkeypatch, [event], [job])

    result = await correlation.correlate_kommo_job("job-1")

    assert result == {"status": "pending"}
    assert fake.events["event-1"]["matched_kommo_job_id"] is None
    assert fake.jobs["job-1"]["meta_context_event_id"] is None


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
async def test_story_uses_matching_receipt_when_combined_message_contains_merged_text(monkeypatch):
    from app.integrations.meta_context import correlation

    event = _event(text="😍", username="cliente")
    event["event_type"] = "story_reply"
    job = _job(text="😍\nPrecio?", username="cliente")
    job["interaction_type"] = "private_message"
    receipt_time = event["event_timestamp"] + timedelta(seconds=1)
    receipt = {
        "id": "receipt-1",
        "external_message_id": "kommo-message-emoji",
        "correlation_timestamp": receipt_time,
        "timestamp_source": "receipt_received_at",
    }
    monkeypatch.setattr(correlation.db, "get_db", lambda: _Handle())
    monkeypatch.setattr(correlation.db, "fetch_one", AsyncMock(return_value={"locked": True}))
    monkeypatch.setattr(correlation, "_load_job", AsyncMock(return_value=job))
    monkeypatch.setattr(correlation, "_candidate_events", AsyncMock(return_value=[event]))
    monkeypatch.setattr(correlation, "_matching_receipts", AsyncMock(return_value=[receipt]))
    monkeypatch.setattr(correlation, "_verified_customer_ids", AsyncMock(return_value=(None, None)))
    async def reciprocal(candidate, **_kwargs):
        return [candidate]

    monkeypatch.setattr(correlation, "_opposite_candidates", reciprocal)
    persist = AsyncMock(return_value=True)
    monkeypatch.setattr(correlation, "_persist_match", persist)
    monkeypatch.setattr(
        correlation,
        "get_config",
        lambda: SimpleNamespace(meta_story_context_match_window_seconds=45),
    )

    result = await correlation.correlate_kommo_job("job-1")

    assert result["status"] == "matched"
    candidate = persist.await_args.args[0]
    assert candidate.text_match_source == "receipt"
    assert candidate.matched_receipt_id == "receipt-1"
    assert candidate.matched_external_message_id == "kommo-message-emoji"
    assert "receipt_text" in candidate.signals


@pytest.mark.asyncio
async def test_multiple_matching_story_receipts_are_ambiguous(monkeypatch):
    from app.integrations.meta_context import correlation

    event = _event(text="😍")
    event["event_type"] = "story_reply"
    job = _job(text="😍\nPrecio?")
    job["interaction_type"] = "private_message"
    receipts = [
        {
            "id": f"receipt-{index}",
            "external_message_id": f"message-{index}",
            "correlation_timestamp": event["event_timestamp"] + timedelta(seconds=index),
            "timestamp_source": "receipt_received_at",
        }
        for index in (1, 2)
    ]
    monkeypatch.setattr(correlation.db, "get_db", lambda: _Handle())
    monkeypatch.setattr(correlation.db, "fetch_one", AsyncMock(return_value={"locked": True}))
    monkeypatch.setattr(correlation, "_load_job", AsyncMock(return_value=job))
    monkeypatch.setattr(correlation, "_candidate_events", AsyncMock(return_value=[event]))
    monkeypatch.setattr(correlation, "_matching_receipts", AsyncMock(return_value=receipts))
    monkeypatch.setattr(correlation, "_verified_customer_ids", AsyncMock(return_value=(None, None)))
    mark = AsyncMock()
    monkeypatch.setattr(correlation, "_mark_ambiguous", mark)
    monkeypatch.setattr(
        correlation,
        "get_config",
        lambda: SimpleNamespace(meta_story_context_match_window_seconds=45),
    )

    result = await correlation.correlate_kommo_job("job-1")

    assert result["status"] == "ambiguous"
    assert result["candidate_count"] == 2
    mark.assert_awaited_once()


@pytest.mark.asyncio
async def test_match_diagnostics_persist_receipt_and_external_message_ids(monkeypatch):
    from app.integrations.meta_context import correlation

    mock_db = AsyncMock()
    mock_db.fetch_one = AsyncMock(return_value={"id": "job-1"})
    mock_db.execute = AsyncMock()
    monkeypatch.setattr(correlation, "db", mock_db)
    event = _event(text="😍")
    event["event_type"] = "story_reply"
    candidate = correlation.Candidate(
        event=event,
        job=_job(text="😍\nPrecio?"),
        score=190,
        timestamp_difference_seconds=1,
        signals=("customer_identity", "receipt_text", "time_5s"),
        matched_receipt_id="receipt-1",
        matched_external_message_id="external-message-1",
        receipt_match_count=1,
        text_match_source="receipt",
        timestamp_source="receipt_received_at",
    )

    assert await correlation._persist_match(candidate) is True

    details = json.loads(mock_db.execute.await_args.args[1]["details"])
    assert details["matched_receipt_id"] == "receipt-1"
    assert details["matched_external_message_id"] == "external-message-1"
    assert details["matched_receipt_timestamp_source"] == "receipt_received_at"
    assert details["text_match_source"] == "receipt"
    assert details["verified_sender_reused"] is True


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
