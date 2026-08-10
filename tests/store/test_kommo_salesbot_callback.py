from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import jwt
import pytest
from app.webhooks import kommo
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

RETURN_URL = "https://acme.kommo.com/api/v4/salesbot/1/continue/2?request_id=secret-query"


def _config(**overrides):
    data = {
        "whatsapp_backend": "kommo",
        "kommo_subdomain": "acme",
        "kommo_integration_secret": "secret",
        "kommo_integration_id": "client-uuid",
        "kommo_webhook_secret": "secret-path",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _token(**claims):
    now = datetime.now(UTC)
    payload = {
        "iss": "https://acme.kommo.com",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "jti": "test-token-id",
        "subdomain": "acme",
        "client_uid": "client-uuid",
        "account_id": 123,
        "entity_type": "lead",
        "entity_id": 100,
    }
    payload.update(claims)
    payload = {key: value for key, value in payload.items() if value is not None}
    return jwt.encode(payload, "secret", algorithm="HS256")


@pytest.fixture
def app(monkeypatch):
    app = FastAPI()
    app.include_router(kommo.router)
    monkeypatch.setattr(kommo, "get_config", lambda: _config())
    monkeypatch.setattr(kommo, "persist_salesbot_callback", AsyncMock(return_value={"status": "ready", "job_id": "job-1"}))
    monkeypatch.setattr(kommo, "process_ready_jobs", AsyncMock(return_value=1))
    return app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _post(client, **kwargs):
    return await client.post("/webhooks/kommo/salesbot", **kwargs)


def _json_body(**overrides):
    body = {
        "token": _token(),
        "return_url": RETURN_URL,
        "data": {
            "message": "Hola secreta",
            "lead_id": "100",
            "contact_id": "200",
            "origin": "whatsapp",
            "expected_channel": "whatsapp",
        },
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_salesbot_callback_accepts_json_body(client):
    response = await _post(client, json=_json_body())
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    callback_data = kommo.persist_salesbot_callback.await_args.args[0]
    assert callback_data.lead_id == "100"
    assert callback_data.message == "Hola secreta"
    assert callback_data.expected_channel == "whatsapp"


@pytest.mark.asyncio
async def test_salesbot_callback_accepts_urlencoded_body(client):
    response = await _post(
        client,
        content=urlencode(
            {
                "token": _token(),
                "return_url": RETURN_URL,
                "data": '{"message":"Hola secreta","lead_id":"100","origin":"whatsapp"}',
            }
        ),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200
    assert kommo.persist_salesbot_callback.await_args.args[0].lead_id == "100"


@pytest.mark.asyncio
async def test_salesbot_callback_accepts_multipart_body(client):
    response = await _post(
        client,
        files={
            "token": (None, _token()),
            "return_url": (None, RETURN_URL),
            "data": (None, '{"lead_id":"100","origin":"whatsapp"}'),
        },
    )
    assert response.status_code == 200
    assert kommo.persist_salesbot_callback.await_args.args[0].lead_id == "100"


@pytest.mark.asyncio
async def test_salesbot_callback_accepts_json_string_data_field(client):
    response = await _post(client, json=_json_body(data='{"lead_id":"100","origin":"whatsapp"}'))
    assert response.status_code == 200
    assert kommo.persist_salesbot_callback.await_args.args[0].origin == "whatsapp"


@pytest.mark.asyncio
async def test_salesbot_callback_accepts_comment_interaction_type(client):
    response = await _post(client, json=_json_body(data={"lead_id": "100", "origin": "instagram", "interaction_type": "instagram_comment"}))
    assert response.status_code == 200
    assert kommo.persist_salesbot_callback.await_args.args[0].interaction_type == "instagram_comment"


@pytest.mark.asyncio
async def test_hybrid_mode_ignores_kommo_instagram_private_salesbot_callback(client, monkeypatch):
    monkeypatch.setattr(
        kommo,
        "get_config",
        lambda: _config(instagram_backend="meta"),
    )

    response = await _post(
        client,
        json=_json_body(
            data={
                "message": "Hola",
                "lead_id": "100",
                "origin": "instagram",
                "expected_channel": "instagram",
            }
        ),
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": "instagram_managed_by_meta",
    }
    kommo.persist_salesbot_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_hybrid_mode_keeps_kommo_whatsapp_salesbot_callback(client, monkeypatch):
    monkeypatch.setattr(
        kommo,
        "get_config",
        lambda: _config(instagram_backend="meta"),
    )

    response = await _post(client, json=_json_body())

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    kommo.persist_salesbot_callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_salesbot_callback_logs_interaction_message_and_signed_entity(client, caplog):
    with caplog.at_level("INFO", logger="app.webhooks.kommo"):
        response = await _post(
            client,
            json=_json_body(data={"message": "Precio?", "lead_id": "100", "origin": "instagram", "interaction_type": "instagram_comment"}),
        )

    assert response.status_code == 200
    assert "interaction_type=instagram_comment" in caplog.text
    assert "message_text_resolved=True" in caplog.text
    assert "signed_entity_type=leads" in caplog.text
    assert "signed_entity_id=100" in caplog.text
    assert "Precio?" not in caplog.text


@pytest.mark.asyncio
async def test_mirrored_comment_general_webhook_creates_private_message_job_for_reconciliation(
    client,
    monkeypatch,
    caplog,
    sanitized_a105_native_instagram_comment_payload,
):
    record = AsyncMock(return_value={"status": "created", "job_id": "private-job"})
    scheduled = AsyncMock()
    monkeypatch.setattr(kommo, "record_incoming_event", record)
    monkeypatch.setattr(kommo, "schedule_due_job_processing", scheduled)

    with caplog.at_level("INFO", logger="app.webhooks.kommo"):
        response = await client.post("/webhooks/kommo/events/secret-path", json=sanitized_a105_native_instagram_comment_payload)

    assert response.status_code == 200
    record.assert_awaited_once()
    scheduled.assert_called_once()
    event = record.await_args.args[0]
    assert event.origin == "instagram_business"
    assert event.message_type == "text"
    assert event.talk_id == "105"
    assert event.interaction_type == "private_message"
    assert "Kommo native Instagram comment ignored by private-message webhook path" not in caplog.text
    assert "Kommo webhook completed" in caplog.text
    assert "statuses={'created': 1}" in caplog.text
    assert "Precio?" not in caplog.text


@pytest.mark.asyncio
async def test_hybrid_mode_ignores_kommo_instagram_general_webhook(
    client,
    monkeypatch,
    sanitized_a105_native_instagram_comment_payload,
):
    monkeypatch.setattr(
        kommo,
        "get_config",
        lambda: _config(instagram_backend="meta"),
    )
    record = AsyncMock()
    monkeypatch.setattr(kommo, "record_incoming_event", record)

    response = await client.post(
        "/webhooks/kommo/events/secret-path",
        json=sanitized_a105_native_instagram_comment_payload,
    )

    assert response.status_code == 200
    record.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("matched", [True, False])
async def test_outgoing_webhook_only_reconciles_existing_delivery(client, monkeypatch, matched):
    confirm = AsyncMock(return_value=matched)
    record = AsyncMock()
    monkeypatch.setattr(kommo, "confirm_outbound_delivery", confirm)
    monkeypatch.setattr(kommo, "record_incoming_event", record)

    response = await client.post(
        "/webhooks/kommo/events/secret-path",
        json={
            "outgoing_message": {
                "add": [
                    {
                        "id": "provider-message-1",
                        "origin": "whatsapp",
                        "author": {"type": "internal"},
                    }
                ]
            }
        },
    )

    assert response.status_code == 200
    confirm.assert_awaited_once_with("provider-message-1")
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_salesbot_callback_accepts_flattened_form_data_fields(client):
    response = await _post(
        client,
        content=urlencode(
            {
                "token": _token(),
                "return_url": RETURN_URL,
                "data[message]": "Hola secreta",
                "data[lead_id]": "100",
                "data[contact_id]": "200",
                "data[origin]": "whatsapp",
            }
        ),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200
    callback_data = kommo.persist_salesbot_callback.await_args.args[0]
    assert callback_data.message == "Hola secreta"
    assert callback_data.lead_id == "100"


@pytest.mark.asyncio
async def test_salesbot_callback_falls_back_for_missing_or_wrong_content_type(client):
    response = await _post(
        client,
        content=urlencode({"token": _token(), "return_url": RETURN_URL, "data[lead_id]": "100"}),
        headers={"content-type": "text/plain"},
    )
    assert response.status_code == 200
    assert kommo.persist_salesbot_callback.await_args.args[0].lead_id == "100"


@pytest.mark.asyncio
async def test_salesbot_callback_missing_body_returns_400_not_401(client):
    response = await _post(client)
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_salesbot_callback_invalid_json_returns_400_not_401(client):
    response = await _post(client, content="{", headers={"content-type": "application/json"})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_salesbot_callback_unsupported_body_shape_returns_400_not_401(client):
    response = await _post(client, json=[])
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_salesbot_callback_parse_logs_exclude_token_return_url_query_and_customer_message(client, caplog):
    token = _token()
    with caplog.at_level("INFO", logger="app.webhooks.kommo"):
        response = await _post(client, json=_json_body(token=token))
    assert response.status_code == 200
    assert token not in caplog.text
    assert "Hola secreta" not in caplog.text
    assert "secret-query" not in caplog.text
    assert "top_level_keys" in caplog.text


@pytest.mark.asyncio
async def test_salesbot_callback_immature_jwt_logs_safe_reason_code_only(client, caplog):
    token = _token(
        nbf=datetime.now(UTC) + timedelta(seconds=30),
        iat=datetime.now(UTC) + timedelta(seconds=30),
    )
    with caplog.at_level("WARNING", logger="app.webhooks.kommo"):
        response = await _post(client, json=_json_body(token=token))
    assert response.status_code == 401
    assert "reason=immature" in caplog.text
    assert token not in caplog.text
    assert "secret" not in caplog.text
    assert "client_uid" not in caplog.text
    assert "client-uuid" not in caplog.text
    assert "account_id" not in caplog.text
    assert RETURN_URL not in caplog.text
    assert "secret-query" not in caplog.text
    assert "Hola secreta" not in caplog.text


@pytest.mark.asyncio
async def test_salesbot_callback_invalid_signature_logs_safe_reason_code_only(client, caplog):
    bad_token = jwt.encode(
        {"subdomain": "acme", "client_uid": "client-uuid", "account_id": 123, "entity_type": "lead", "entity_id": 100},
        "wrong-secret",
        algorithm="HS256",
    )
    with caplog.at_level("WARNING", logger="app.webhooks.kommo"):
        response = await _post(client, json=_json_body(token=bad_token))
    assert response.status_code == 401
    assert "reason=invalid_signature" in caplog.text
    assert bad_token not in caplog.text
    assert "wrong-secret" not in caplog.text


@pytest.mark.asyncio
async def test_salesbot_callback_invalid_jwt_logs_safe_reason_code_only(client, caplog):
    bad_token = _token(client_uid="other")
    with caplog.at_level("WARNING", logger="app.webhooks.kommo"):
        response = await _post(client, json=_json_body(token=bad_token))
    assert response.status_code == 401
    assert "reason=integration_mismatch" in caplog.text
    assert bad_token not in caplog.text
    assert "secret" not in caplog.text
    assert RETURN_URL not in caplog.text
    assert "secret-query" not in caplog.text
    assert "client_uid" not in caplog.text
    assert "other" not in caplog.text


@pytest.mark.asyncio
async def test_salesbot_callback_invalid_return_url_logs_safe_reason_code_only(client, caplog):
    return_url = "https://evil.example/api/v4/salesbot/1/continue/2?secret=query"
    with caplog.at_level("WARNING", logger="app.webhooks.kommo"):
        response = await _post(client, json=_json_body(return_url=return_url))
    assert response.status_code == 401
    assert "reason=invalid_return_url" in caplog.text
    assert return_url not in caplog.text
    assert "secret=query" not in caplog.text


@pytest.mark.asyncio
async def test_salesbot_callback_expired_jwt_logs_safe_reason_code_only(client, caplog):
    token = _token(exp=datetime.now(UTC) - timedelta(minutes=1))
    with caplog.at_level("WARNING", logger="app.webhooks.kommo"):
        response = await _post(client, json=_json_body(token=token))
    assert response.status_code == 401
    assert "reason=expired_token" in caplog.text
    assert token not in caplog.text
