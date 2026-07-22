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
