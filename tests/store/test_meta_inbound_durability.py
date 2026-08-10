import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _database():
    database = MagicMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    database.transaction.return_value = transaction
    return database


def _close_background_task(coro):
    coro.close()
    return MagicMock()


@pytest.mark.asyncio
async def test_due_job_claim_failure_releases_processor_slot(monkeypatch):
    from app.webhooks import inbound_buffer

    semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(inbound_buffer, "_inbound_processor_semaphore", semaphore)
    monkeypatch.setattr(inbound_buffer, "recover_stale_inbound_jobs", AsyncMock(return_value=0))
    monkeypatch.setattr(inbound_buffer, "_claim_due_job", AsyncMock(side_effect=RuntimeError("database unavailable")))

    with pytest.raises(RuntimeError, match="database unavailable"):
        await inbound_buffer.process_due_inbound_jobs(limit=1)

    assert not semaphore.locked()


@pytest.mark.asyncio
async def test_enqueue_commits_dedup_receipt_before_returning_success():
    from app.webhooks import inbound_buffer

    execute = AsyncMock()
    with (
        patch.object(inbound_buffer.db, "get_db", return_value=_database()),
        patch.object(
            inbound_buffer.db,
            "fetch_one",
            AsyncMock(side_effect=[None, None, None, {"id": "job-1"}]),
        ),
        patch.object(inbound_buffer.db, "execute", execute),
        patch.object(inbound_buffer.asyncio, "create_task", side_effect=_close_background_task),
    ):
        created = await inbound_buffer.enqueue_inbound_message(
            channel="instagram",
            sender_id="sender-1",
            message_id="mid-1",
            text="Hola",
            customer_profile={"display_name": "Ana"},
        )

    assert created is True
    receipt_query, receipt_values = execute.await_args.args
    assert "INSERT INTO meta_inbound_receipts" in receipt_query
    assert receipt_values == {"channel": "instagram", "message_id": "mid-1", "job_id": "job-1"}


@pytest.mark.asyncio
async def test_duplicate_meta_message_id_is_ignored_durably():
    from app.webhooks import inbound_buffer

    execute = AsyncMock()
    with (
        patch.object(inbound_buffer.db, "get_db", return_value=_database()),
        patch.object(inbound_buffer.db, "fetch_one", AsyncMock(side_effect=[None, {"id": "receipt-1"}])),
        patch.object(inbound_buffer.db, "execute", execute),
        patch.object(inbound_buffer.asyncio, "create_task") as create_task,
    ):
        created = await inbound_buffer.enqueue_inbound_message(
            channel="instagram",
            sender_id="sender-1",
            message_id="mid-1",
            text="Hola",
        )

    assert created is False
    execute.assert_not_awaited()
    create_task.assert_not_called()


@pytest.mark.asyncio
async def test_rapid_messages_merge_in_persisted_pending_batch():
    from app.webhooks import inbound_buffer

    pending = {
        "id": "job-1",
        "message_parts": ["Hola"],
        "customer_profile": {"display_name": "Ana"},
    }
    execute = AsyncMock()
    with (
        patch.object(inbound_buffer.db, "get_db", return_value=_database()),
        patch.object(inbound_buffer.db, "fetch_one", AsyncMock(side_effect=[None, None, pending])),
        patch.object(inbound_buffer.db, "execute", execute),
        patch.object(inbound_buffer.asyncio, "create_task", side_effect=_close_background_task),
    ):
        created = await inbound_buffer.enqueue_inbound_message(
            channel="instagram",
            sender_id="sender-1",
            message_id="mid-2",
            text="Tienen pijamas?",
            customer_profile={"instagram_handle": "ana"},
        )

    assert created is True
    merge_values = execute.await_args_list[0].args[1]
    assert json.loads(merge_values["parts"]) == ["Hola", "Tienen pijamas?"]
    assert json.loads(merge_values["profile"]) == {"display_name": "Ana", "instagram_handle": "ana"}


@pytest.mark.asyncio
async def test_active_customer_lease_prevents_concurrent_ai_turn():
    from app.webhooks import inbound_buffer

    with (
        patch.object(inbound_buffer.db, "get_db", return_value=_database()),
        patch.object(inbound_buffer.db, "fetch_one", AsyncMock(side_effect=[None, {"id": "active-job"}])),
    ):
        claimed = await inbound_buffer._claim_sender_job("whatsapp", "sender-1")

    assert claimed is None


@pytest.mark.asyncio
async def test_claimed_job_completion_is_fenced_by_lease_token():
    from app.webhooks import inbound_buffer

    job = {
        "id": "job-1",
        "channel": "whatsapp",
        "sender_id": "sender-1",
        "message_parts": ["Hola"],
        "media_url": None,
        "customer_profile": {},
        "attempt_count": 1,
        "processing_lease_token": "lease-1",
    }
    processor = AsyncMock(return_value=None)
    fetch_one = AsyncMock(return_value=None)

    with (
        patch.object(inbound_buffer, "_resolve_processor", return_value=processor),
        patch.object(inbound_buffer.db, "fetch_one", fetch_one),
        patch.object(inbound_buffer.db, "execute", AsyncMock()),
    ):
        await inbound_buffer._process_claimed_job(job)

    processor.assert_awaited_once_with("sender-1", "Hola", None, {}, "job-1", "lease-1")
    completion_query, completion_values = fetch_one.await_args.args
    assert "processing_lease_token = :lease_token" in completion_query
    assert completion_values == {"job_id": "job-1", "lease_token": "lease-1"}


@pytest.mark.asyncio
async def test_stale_processing_lease_returns_to_pending_after_crash():
    from app.webhooks import inbound_buffer

    stale = {"id": "job-1", "channel": "whatsapp", "sender_id": "sender-1"}
    stale_job = {
        "id": "job-1",
        "message_parts": ["Hola"],
        "media_url": None,
        "customer_profile": {},
        "attempt_count": 1,
    }
    execute = AsyncMock()
    with (
        patch.object(inbound_buffer.db, "fetch_all", AsyncMock(return_value=[stale])),
        patch.object(inbound_buffer.db, "get_db", return_value=_database()),
        patch.object(inbound_buffer.db, "fetch_one", AsyncMock(side_effect=[None, stale_job, None])),
        patch.object(inbound_buffer.db, "execute", execute),
    ):
        recovered = await inbound_buffer.recover_stale_inbound_jobs()

    assert recovered == 1
    assert "status = 'pending'" in execute.await_args.args[0]
    assert "stale_lease_recovered" in execute.await_args.args[0]


@pytest.mark.asyncio
async def test_stale_processing_lease_with_outbound_id_completes_instead_of_requeue():
    from app.webhooks import inbound_buffer

    stale = {"id": "job-1", "channel": "whatsapp", "sender_id": "sender-1"}
    stale_job = {
        "id": "job-1",
        "message_parts": ["Hola"],
        "media_url": None,
        "customer_profile": {},
        "attempt_count": 1,
        "outbound_started_at": "now",
        "outbound_message_ids": ["wamid.accepted"],
    }
    execute = AsyncMock()
    with (
        patch.object(inbound_buffer.db, "fetch_all", AsyncMock(return_value=[stale])),
        patch.object(inbound_buffer.db, "get_db", return_value=_database()),
        patch.object(inbound_buffer.db, "fetch_one", AsyncMock(side_effect=[None, stale_job])),
        patch.object(inbound_buffer.db, "execute", execute),
    ):
        recovered = await inbound_buffer.recover_stale_inbound_jobs()

    assert recovered == 1
    assert "status = 'completed'" in execute.await_args.args[0]
    assert "stale_lease_completed_after_meta_accept" in execute.await_args.args[0]


@pytest.mark.asyncio
async def test_stale_processing_lease_with_uncertain_send_is_not_requeued():
    from app.webhooks import inbound_buffer

    stale = {"id": "job-1", "channel": "instagram", "sender_id": "sender-1"}
    stale_job = {
        "id": "job-1",
        "message_parts": ["Hola"],
        "media_url": None,
        "customer_profile": {},
        "attempt_count": 1,
        "outbound_started_at": "now",
        "outbound_message_ids": [],
    }
    execute = AsyncMock()
    with (
        patch.object(inbound_buffer.db, "fetch_all", AsyncMock(return_value=[stale])),
        patch.object(inbound_buffer.db, "get_db", return_value=_database()),
        patch.object(inbound_buffer.db, "fetch_one", AsyncMock(side_effect=[None, stale_job])),
        patch.object(inbound_buffer.db, "execute", execute),
    ):
        recovered = await inbound_buffer.recover_stale_inbound_jobs()

    assert recovered == 1
    assert "status = 'failed'" in execute.await_args.args[0]
    assert "delivery_unknown_after_stale_lease" in execute.await_args.args[0]


@pytest.mark.asyncio
async def test_failed_active_turn_merges_ahead_of_newer_pending_messages():
    from app.webhooks import inbound_buffer

    job = {
        "id": "active-job",
        "channel": "instagram",
        "sender_id": "sender-1",
        "attempt_count": 1,
    }
    current = {
        "id": "active-job",
        "message_parts": ["primero"],
        "media_url": None,
        "customer_profile": {"display_name": "Ana"},
    }
    pending = {
        "id": "pending-job",
        "message_parts": ["después"],
        "media_url": None,
        "customer_profile": {"instagram_handle": "ana"},
    }
    execute = AsyncMock()
    with (
        patch.object(inbound_buffer.db, "get_db", return_value=_database()),
        patch.object(inbound_buffer.db, "fetch_one", AsyncMock(side_effect=[None, current, pending])),
        patch.object(inbound_buffer.db, "execute", execute),
    ):
        await inbound_buffer._requeue_failed_job(job, "RuntimeError")

    merged_values = execute.await_args_list[0].args[1]
    assert json.loads(merged_values["parts"]) == ["primero", "después"]
    assert execute.await_count == 2


@pytest.mark.asyncio
async def test_delivery_failure_is_requeued_not_completed():
    from app.webhooks import inbound_buffer

    job = {
        "id": "job-1",
        "channel": "whatsapp",
        "sender_id": "sender-1",
        "message_parts": ["Hola"],
        "media_url": None,
        "customer_profile": {},
        "attempt_count": 1,
        "processing_lease_token": "lease-1",
    }
    processor = AsyncMock(side_effect=RuntimeError("send failed"))
    requeue = AsyncMock()
    with (
        patch.object(inbound_buffer, "_resolve_processor", return_value=processor),
        patch.object(inbound_buffer, "_requeue_failed_job", requeue),
        patch.object(inbound_buffer.db, "fetch_one", AsyncMock()),
        patch.object(inbound_buffer.db, "execute", AsyncMock()),
    ):
        await inbound_buffer._process_claimed_job(job)

    requeue.assert_awaited_once()
    assert requeue.await_args.args[0] == job
    assert isinstance(requeue.await_args.args[1], RuntimeError)


@pytest.mark.asyncio
async def test_whatsapp_send_failure_propagates_after_notification():
    from app.webhooks import whatsapp

    result = {"customer_id": "customer-1", "text": "Hola"}
    with (
        patch.object(whatsapp, "generate_response", AsyncMock(return_value=result)),
        patch.object(whatsapp, "send_with_delivery_record", AsyncMock(side_effect=RuntimeError("meta down"))),
        patch.object(whatsapp, "_notify_delivery_failure", AsyncMock()) as notify,
        pytest.raises(RuntimeError, match="meta down"),
    ):
        await whatsapp._deliver_ai_response("sender-1", "Hola", None, {}, "job-1", "lease-1")

    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_whatsapp_refuses_send_when_lease_token_is_stale():
    from app.webhooks import inbound_buffer

    send_text = AsyncMock(return_value={"messages": [{"id": "wamid.1"}]})
    with (
        patch.object(inbound_buffer, "mark_outbound_send_started", AsyncMock(return_value=False)),
        patch.object(inbound_buffer, "record_outbound_message", AsyncMock()) as record,
        pytest.raises(RuntimeError, match="lease is no longer active"),
    ):
        await inbound_buffer.send_with_delivery_record(
            send_text, "job-1", "old-lease", to="sender-1", text="Hola"
        )

    send_text.assert_not_awaited()
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_recovery_rechecks_lease_token_and_heartbeat_after_sender_lock():
    from app.webhooks import inbound_buffer

    stale = {
        "id": "job-1",
        "channel": "whatsapp",
        "sender_id": "sender-1",
        "processing_lease_token": "old-lease",
    }
    execute = AsyncMock()
    fetch_one = AsyncMock(side_effect=[None, None])
    with (
        patch.object(inbound_buffer.db, "fetch_all", AsyncMock(return_value=[stale])),
        patch.object(inbound_buffer.db, "get_db", return_value=_database()),
        patch.object(inbound_buffer.db, "fetch_one", fetch_one),
        patch.object(inbound_buffer.db, "execute", execute),
    ):
        recovered = await inbound_buffer.recover_stale_inbound_jobs()

    assert recovered == 0
    stale_query, stale_values = fetch_one.await_args_list[1].args
    assert "processing_lease_token IS NOT DISTINCT FROM :lease_token" in stale_query
    assert "processing_heartbeat_at" in stale_query
    assert stale_values["lease_token"] == "old-lease"
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_meta_send_failure_retries_before_requeueing_job():
    from app.channels.meta_errors import MetaSendError
    from app.webhooks import inbound_buffer

    send = AsyncMock(side_effect=[
        MetaSendError("rate limited", retryable=True),
        {"messages": [{"id": "wamid.1"}]},
    ])
    with (
        patch.object(inbound_buffer, "mark_outbound_send_started", AsyncMock(return_value=True)),
        patch.object(inbound_buffer, "record_outbound_message", AsyncMock()) as record,
        patch.object(inbound_buffer.asyncio, "sleep", AsyncMock()) as sleep,
    ):
        response = await inbound_buffer.send_with_delivery_record(
            send, "job-1", "lease-1", to="sender-1", text="Hola"
        )

    assert response == {"messages": [{"id": "wamid.1"}]}
    assert send.await_count == 2
    sleep.assert_awaited_once_with(1)
    record.assert_awaited_once_with("job-1", "lease-1", response)


@pytest.mark.asyncio
async def test_heartbeat_continues_after_a_transient_database_error():
    from app.webhooks import inbound_buffer

    sleep_calls = 0

    async def sleep_then_cancel(_):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 3:
            raise asyncio.CancelledError

    execute = AsyncMock(side_effect=[RuntimeError("database unavailable"), None])
    with (
        patch.object(inbound_buffer.asyncio, "sleep", sleep_then_cancel),
        patch.object(inbound_buffer.db, "execute", execute),
        pytest.raises(asyncio.CancelledError),
    ):
        await inbound_buffer._heartbeat_claimed_job("job-1", "lease-1")

    assert execute.await_count == 2


@pytest.mark.asyncio
async def test_instagram_uses_meta_mid_for_durable_deduplication():
    from app.webhooks import instagram

    enqueue = AsyncMock(return_value=True)
    event = {
        "sender": {"id": "ig-user"},
        "message": {"mid": "ig-mid-123", "text": "Hola"},
    }
    with patch.object(instagram, "enqueue_inbound_message", enqueue):
        await instagram._process_event(event)

    assert enqueue.await_args.kwargs["message_id"] == "ig-mid-123"
    assert enqueue.await_args.kwargs["channel"] == "instagram"


@pytest.mark.asyncio
async def test_native_meta_instagram_webhook_enqueues_dm():
    from app.webhooks import instagram

    payload = {
        "object": "instagram",
        "entry": [{
            "messaging": [{
                "sender": {"id": "ig-user"},
                "message": {"mid": "ig-mid-456", "text": "Hola"},
            }]
        }],
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    secret = "meta-secret"
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    app = FastAPI()
    app.include_router(instagram.router)
    enqueue = AsyncMock(return_value=True)

    with (
        patch.object(instagram, "get_config", return_value=SimpleNamespace(meta_app_secret=secret)),
        patch.object(instagram, "enqueue_inbound_message", enqueue),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/webhooks/instagram",
                content=body,
                headers={
                    "content-type": "application/json",
                    "X-Hub-Signature-256": signature,
                },
            )

    assert response.status_code == 200
    enqueue.assert_awaited_once()
    assert enqueue.await_args.kwargs["channel"] == "instagram"
    assert enqueue.await_args.kwargs["message_id"] == "ig-mid-456"


@pytest.mark.asyncio
async def test_whatsapp_returns_failure_when_durable_enqueue_fails():
    from app.webhooks import whatsapp

    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid-1",
                        "from": "584121234567",
                        "type": "text",
                        "text": {"body": "Hola"},
                    }]
                }
            }]
        }],
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    secret = "meta-secret"
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    app = FastAPI()
    app.include_router(whatsapp.router)

    with (
        patch.object(whatsapp, "get_config", return_value=SimpleNamespace(meta_app_secret=secret)),
        patch.object(whatsapp, "enqueue_inbound_message", AsyncMock(side_effect=RuntimeError("db down"))),
        patch.object(whatsapp, "mark_as_read", AsyncMock()) as mark_as_read,
    ):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/webhooks/whatsapp",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": signature,
                },
            )

    assert response.status_code == 500
    mark_as_read.assert_not_awaited()
