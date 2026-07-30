import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _signature(body: bytes, secret: str = "context-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _comment_payload():
    return {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-account",
                "time": 1_700_000_000,
                "changes": [
                    {
                        "field": "comments",
                        "value": {
                            "id": "comment-1",
                            "parent_id": "parent-1",
                            "text": "  Precio? ",
                            "from": {"id": "sender-1", "username": "Client.One"},
                            "media": {"id": "media-1", "media_product_type": "REELS"},
                        },
                    }
                ],
            }
        ],
    }


def _client(monkeypatch):
    from app.webhooks import meta_instagram_context as webhook

    app = FastAPI()
    app.include_router(webhook.router)
    config = SimpleNamespace(
        meta_instagram_context_enabled=True,
        instagram_verify_token="verify-token",
        meta_app_secret="context-secret",
        instagram_account_id="ig-account",
        outbound_processing_enabled=True,
    )
    monkeypatch.setattr(webhook, "get_config", lambda: config)
    return TestClient(app), webhook


def test_meta_context_webhook_verification(monkeypatch):
    client, _ = _client(monkeypatch)

    response = client.get(
        "/webhooks/meta/instagram-context",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-token",
            "hub.challenge": "challenge-value",
        },
    )
    assert response.status_code == 200
    assert response.text == "challenge-value"

    rejected = client.get(
        "/webhooks/meta/instagram-context",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "x"},
    )
    assert rejected.status_code == 403


def test_meta_context_webhook_accepts_valid_signature_and_schedules_context_only_work(monkeypatch):
    client, webhook = _client(monkeypatch)
    store = AsyncMock(return_value={"status": "created", "event_id": "event-1"})
    process = AsyncMock()
    monkeypatch.setattr(webhook, "store_context_event", store)
    monkeypatch.setattr(webhook, "process_context_event", process)
    body = json.dumps(_comment_payload()).encode()

    response = client.post(
        "/webhooks/meta/instagram-context",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _signature(body)},
    )

    assert response.status_code == 200
    event = store.await_args.args[0]
    assert event.comment_id == "comment-1"
    assert event.parent_comment_id == "parent-1"
    assert event.media_id == "media-1"
    assert event.sender_username == "Client.One"
    process.assert_awaited_once_with("event-1")
    assert not hasattr(webhook, "generate_response")
    assert not hasattr(webhook, "send_text")
    assert not hasattr(webhook, "send_private_reply")


def test_meta_context_webhook_ignores_events_for_different_account(monkeypatch):
    client, webhook = _client(monkeypatch)
    store = AsyncMock()
    monkeypatch.setattr(webhook, "store_context_event", store)
    payload = _comment_payload()
    payload["entry"][0]["id"] = "other-account"
    body = json.dumps(payload).encode()

    response = client.post(
        "/webhooks/meta/instagram-context",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _signature(body)},
    )

    assert response.status_code == 200
    store.assert_not_awaited()


def test_meta_context_webhook_never_starts_ready_job_processor(monkeypatch):
    client, webhook = _client(monkeypatch)
    store = AsyncMock(return_value={"status": "created", "event_id": "event-1"})
    enrich = AsyncMock()
    monkeypatch.setattr(webhook, "store_context_event", store)
    monkeypatch.setattr(webhook, "process_context_event", enrich)
    body = json.dumps(_comment_payload()).encode()

    response = client.post(
        "/webhooks/meta/instagram-context",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _signature(body)},
    )

    assert response.status_code == 200
    enrich.assert_awaited_once_with("event-1")
    assert not hasattr(webhook, "process_ready_jobs")


@pytest.mark.parametrize("signature", ["", "sha256=bad"])
def test_meta_context_webhook_rejects_invalid_signatures(monkeypatch, signature):
    client, webhook = _client(monkeypatch)
    store = AsyncMock()
    monkeypatch.setattr(webhook, "store_context_event", store)
    body = json.dumps(_comment_payload()).encode()

    response = client.post(
        "/webhooks/meta/instagram-context",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )

    assert response.status_code == 403
    store.assert_not_awaited()


def test_comment_parser_supports_entry_value_and_missing_optional_fields():
    from app.integrations.meta_context.parser import parse_instagram_comment_events

    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-account",
                "field": "comments",
                "value": {
                    "comment_id": "comment-2",
                    "message": "Disponible?",
                    "media_id": "media-2",
                },
            }
        ],
    }

    events = parse_instagram_comment_events(payload)

    assert len(events) == 1
    assert events[0].comment_id == "comment-2"
    assert events[0].message_text == "Disponible?"
    assert events[0].media_id == "media-2"
    assert events[0].sender_id is None
    assert events[0].sender_username is None


@pytest.mark.asyncio
async def test_duplicate_meta_delivery_returns_existing_event(monkeypatch):
    from app.integrations.meta_context import service
    from app.integrations.meta_context.models import MetaInstagramContextEvent

    mock_db = MagicMock()
    mock_db.fetch_one = AsyncMock(side_effect=[None, {"id": "existing-event"}])
    monkeypatch.setattr(service, "db", mock_db)
    monkeypatch.setattr(
        service,
        "get_config",
        lambda: SimpleNamespace(
            meta_context_event_retention_hours=24,
        ),
    )

    result = await service.store_context_event(
        MetaInstagramContextEvent(
            external_event_id="comment-1",
            comment_id="comment-1",
            message_text="Precio?",
        )
    )

    assert result == {"status": "duplicate", "event_id": "existing-event"}
    insert_query, insert_values = mock_db.fetch_one.await_args_list[0].args
    assert "ON CONFLICT DO NOTHING" in insert_query
    assert ":retention_hours * INTERVAL '1 hour'" in insert_query
    assert insert_values["retention_hours"] == 24
