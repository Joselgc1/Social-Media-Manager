from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _response(**overrides):
    result = {
        "text": "Respuesta de Eva",
        "interactive": None,
        "catalog_pdf": None,
        "product_image": None,
        "customer_id": "customer-1",
        "escalated": False,
    }
    result.update(overrides)
    return result


@pytest.mark.asyncio
async def test_manual_takeover_mid_generation_suppresses_first_meta_send():
    from app.webhooks import instagram
    from app.webhooks.inbound_buffer import AutomationDeliverySuppressed

    send = AsyncMock()
    store = AsyncMock()
    with (
        patch.object(instagram, "generate_response", AsyncMock(return_value=_response())),
        patch.object(
            instagram,
            "ensure_instagram_ai_delivery_allowed",
            AsyncMock(side_effect=AutomationDeliverySuppressed("manual takeover")),
        ),
        patch.object(instagram, "send_text", send),
        patch.object(instagram.conversations, "store_message", store),
    ):
        await instagram._deliver_ai_response("ig-user", "Hola")

    send.assert_not_awaited()
    store.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_takeover_suppresses_generation_error_apology():
    from app.crm import channel_mappings
    from app.webhooks import instagram
    from app.webhooks.inbound_buffer import AutomationDeliverySuppressed

    send = AsyncMock()
    with (
        patch.object(instagram, "generate_response", AsyncMock(side_effect=RuntimeError("LLM failed"))),
        patch.object(
            channel_mappings,
            "lookup_meta_instagram_sender",
            AsyncMock(return_value={"customer_id": "customer-1"}),
        ),
        patch.object(
            instagram,
            "ensure_instagram_ai_delivery_allowed",
            AsyncMock(side_effect=AutomationDeliverySuppressed("manual takeover")),
        ),
        patch.object(instagram, "send_text", send),
    ):
        await instagram._deliver_ai_response("ig-user", "Hola")

    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmapped_generation_error_apology_still_runs_recipient_guard():
    from app.crm import channel_mappings
    from app.webhooks import instagram
    from app.webhooks.inbound_buffer import AutomationDeliverySuppressed

    guard = AsyncMock(side_effect=AutomationDeliverySuppressed("AI paused"))
    send = AsyncMock()
    with (
        patch.object(instagram, "generate_response", AsyncMock(side_effect=RuntimeError("LLM failed"))),
        patch.object(channel_mappings, "lookup_meta_instagram_sender", AsyncMock(return_value=None)),
        patch.object(instagram, "ensure_instagram_ai_delivery_allowed", guard),
        patch.object(instagram, "send_text", send),
    ):
        await instagram._deliver_ai_response("ig-user", "Hola")

    assert guard.await_args.args == (None,)
    assert guard.await_args.kwargs["recipient_id"] == "ig-user"
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_turn_automatic_handoff_is_allowed():
    from app.webhooks import instagram

    guard = AsyncMock()
    send = AsyncMock(return_value={"message_id": "mid-handoff"})
    with (
        patch.object(
            instagram,
            "generate_response",
            AsyncMock(return_value=_response(text="Te paso con una persona", escalated=True)),
        ),
        patch.object(instagram, "ensure_instagram_ai_delivery_allowed", guard),
        patch.object(instagram, "send_text", send),
        patch.object(instagram.conversations, "store_message", AsyncMock()),
    ):
        await instagram._deliver_ai_response("ig-user", "Humano")

    send.assert_awaited_once()
    assert guard.await_args.args == ("customer-1",)
    assert guard.await_args.kwargs["recipient_id"] == "ig-user"
    assert guard.await_args.kwargs["allow_current_automatic_escalation"] is True
    assert isinstance(guard.await_args.kwargs["current_turn_started_at"], datetime)


@pytest.mark.asyncio
async def test_takeover_between_image_and_text_stops_later_delivery():
    from app.webhooks import instagram
    from app.webhooks.inbound_buffer import AutomationDeliverySuppressed

    guard = AsyncMock(side_effect=[None, AutomationDeliverySuppressed("human replied")])
    send_image = AsyncMock(return_value={"message_id": "mid-image"})
    send_text = AsyncMock()
    store = AsyncMock()
    with (
        patch.object(
            instagram,
            "generate_response",
            AsyncMock(
                return_value=_response(
                    text="Aquí tienes la foto",
                    product_image={"type": "product_image", "image_url": "https://cdn.example/p.jpg"},
                )
            ),
        ),
        patch.object(instagram, "ensure_instagram_ai_delivery_allowed", guard),
        patch.object(instagram, "send_image", send_image),
        patch.object(instagram, "send_text", send_text),
        patch.object(instagram.conversations, "store_message", store),
    ):
        await instagram._deliver_ai_response("ig-user", "Foto")

    send_image.assert_awaited_once()
    send_text.assert_not_awaited()
    assert store.await_args.kwargs["content"] == "[Imagen de producto enviada]"


@pytest.mark.asyncio
async def test_echo_before_backend_id_persistence_is_durably_deferred(monkeypatch):
    from app.webhooks import instagram

    monkeypatch.setattr(
        instagram,
        "get_config",
        lambda: SimpleNamespace(instagram_account_id="ig-account"),
    )
    monkeypatch.setattr(
        instagram,
        "is_recorded_meta_outbound_message",
        AsyncMock(return_value=False),
    )
    defer = AsyncMock(return_value=True)
    manual = AsyncMock()
    monkeypatch.setattr(instagram, "defer_unknown_instagram_outbound_echo", defer)
    monkeypatch.setattr(instagram, "_record_manual_outbound_echo", manual)

    await instagram._process_outbound_echo({
        "sender": {"id": "ig-account"},
        "recipient": {"id": "ig-user"},
        "message": {"mid": "mid-race", "text": "Respuesta", "is_echo": True},
    })

    defer.assert_awaited_once_with(
        message_id="mid-race",
        recipient_id="ig-user",
        message_text="Respuesta",
    )
    manual.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_echo_becomes_backend_after_id_is_recorded(monkeypatch):
    from app.webhooks import instagram

    monkeypatch.setattr(
        instagram,
        "claim_pending_instagram_outbound_echoes",
        AsyncMock(
            return_value=[{
                "provider_message_id": "mid-race",
                "recipient_id": "ig-user",
                "message_text": "Respuesta",
            }]
        ),
    )
    monkeypatch.setattr(
        instagram,
        "is_recorded_meta_outbound_message",
        AsyncMock(return_value=True),
    )
    mark = AsyncMock()
    manual = AsyncMock()
    monkeypatch.setattr(instagram, "mark_instagram_outbound_echo", mark)
    monkeypatch.setattr(instagram, "_record_manual_outbound_echo", manual)

    assert await instagram.reconcile_pending_instagram_outbound_echoes() == 1
    mark.assert_awaited_once_with("mid-race", "backend")
    manual.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivery_unknown_echo_pauses_without_recording_a_human_message(monkeypatch):
    from app.webhooks import instagram

    monkeypatch.setattr(
        instagram,
        "claim_pending_instagram_outbound_echoes",
        AsyncMock(return_value=[{
            "provider_message_id": "mid-unknown",
            "recipient_id": "ig-user",
            "message_text": "Respuesta posiblemente automatizada",
        }]),
    )
    monkeypatch.setattr(instagram, "is_recorded_meta_outbound_message", AsyncMock(return_value=False))
    monkeypatch.setattr(instagram, "is_recent_instagram_outbound_inflight", AsyncMock(return_value=False))
    monkeypatch.setattr(instagram, "has_unknown_instagram_outbound_delivery", AsyncMock(return_value=True))
    record_unknown = AsyncMock()
    record_manual = AsyncMock()
    mark = AsyncMock()
    monkeypatch.setattr(instagram, "_record_unknown_outbound_echo", record_unknown)
    monkeypatch.setattr(instagram, "_record_manual_outbound_echo", record_manual)
    monkeypatch.setattr(instagram, "mark_instagram_outbound_echo", mark)

    assert await instagram.reconcile_pending_instagram_outbound_echoes() == 1

    record_unknown.assert_awaited_once_with(recipient_id="ig-user")
    record_manual.assert_not_awaited()
    mark.assert_awaited_once_with("mid-unknown", "unknown")


@pytest.mark.asyncio
async def test_deferred_echo_rechecks_backend_id_after_recipient_lock(monkeypatch):
    from app.webhooks import inbound_buffer

    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    database = MagicMock()
    database.transaction.return_value = transaction
    monkeypatch.setattr(inbound_buffer.db, "get_db", lambda: database)
    monkeypatch.setattr(inbound_buffer, "_lock_sender", AsyncMock())
    monkeypatch.setattr(
        inbound_buffer,
        "is_recorded_meta_outbound_message",
        AsyncMock(return_value=True),
    )
    execute = AsyncMock()
    monkeypatch.setattr(inbound_buffer.db, "execute", execute)

    assert await inbound_buffer.defer_unknown_instagram_outbound_echo(
        message_id="mid-backend",
        recipient_id="ig-user",
        message_text="Respuesta",
    )

    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivery_guard_rejects_manual_state_but_allows_current_automatic(monkeypatch):
    from app.webhooks import inbound_buffer

    monkeypatch.setattr(inbound_buffer.db, "get_settings", AsyncMock(return_value={"ai_enabled": True}))
    turn_started_at = datetime.now(UTC)
    fetch = AsyncMock(
        side_effect=[
            {
                "conversation_state": "escalated",
                "escalation_source": "manual",
                "escalated_at": turn_started_at,
                "is_blocked": False,
            },
            {
                "conversation_state": "escalated",
                "escalation_source": "automatic",
                "escalated_at": turn_started_at + timedelta(seconds=1),
                "is_blocked": False,
            },
        ]
    )
    monkeypatch.setattr(inbound_buffer.db, "fetch_one", fetch)

    with pytest.raises(inbound_buffer.AutomationDeliverySuppressed):
        await inbound_buffer.ensure_instagram_ai_delivery_allowed(
            "customer-1",
            allow_current_automatic_escalation=True,
            current_turn_started_at=turn_started_at,
        )

    await inbound_buffer.ensure_instagram_ai_delivery_allowed(
        "customer-1",
        allow_current_automatic_escalation=True,
        current_turn_started_at=turn_started_at,
    )


@pytest.mark.asyncio
async def test_delivery_guard_rejects_automatic_escalation_from_an_older_turn(monkeypatch):
    from app.webhooks import inbound_buffer

    turn_started_at = datetime.now(UTC)
    monkeypatch.setattr(inbound_buffer.db, "get_settings", AsyncMock(return_value={"ai_enabled": True}))
    monkeypatch.setattr(
        inbound_buffer.db,
        "fetch_one",
        AsyncMock(return_value={
            "conversation_state": "escalated",
            "escalation_source": "automatic",
            "escalated_at": turn_started_at - timedelta(seconds=1),
            "is_blocked": False,
        }),
    )

    with pytest.raises(inbound_buffer.AutomationDeliverySuppressed):
        await inbound_buffer.ensure_instagram_ai_delivery_allowed(
            "customer-1",
            allow_current_automatic_escalation=True,
            current_turn_started_at=turn_started_at,
        )


@pytest.mark.asyncio
async def test_pending_unknown_echo_suppresses_next_delivery(monkeypatch):
    from app.webhooks import inbound_buffer

    monkeypatch.setattr(inbound_buffer.db, "get_settings", AsyncMock(return_value={"ai_enabled": True}))
    monkeypatch.setattr(
        inbound_buffer.db,
        "fetch_one",
        AsyncMock(return_value={"provider_message_id": "mid-manual"}),
    )

    with pytest.raises(inbound_buffer.AutomationDeliverySuppressed, match="Unresolved"):
        await inbound_buffer.ensure_instagram_ai_delivery_allowed(
            "customer-1",
            recipient_id="ig-user",
            allow_current_automatic_escalation=False,
        )


@pytest.mark.asyncio
async def test_instagram_send_marker_commits_before_locked_provider_send(monkeypatch):
    from app.webhooks import inbound_buffer

    transaction = MagicMock()
    events = []
    transaction.__aenter__ = AsyncMock(side_effect=lambda: events.append("enter"))
    transaction.__aexit__ = AsyncMock(side_effect=lambda *_: events.append("exit"))
    database = MagicMock()
    database.transaction.return_value = transaction
    guard = AsyncMock(side_effect=lambda: events.append("guard"))
    lock = AsyncMock(side_effect=lambda *_: events.append("lock"))
    mark = AsyncMock(side_effect=lambda *_: events.append("mark") or True)
    send = AsyncMock(side_effect=lambda **_: events.append("send") or {"message_id": "mid-1"})
    record = AsyncMock(side_effect=lambda *_: events.append("record"))
    monkeypatch.setattr(inbound_buffer.db, "get_db", lambda: database)
    monkeypatch.setattr(inbound_buffer, "_lock_sender", lock)
    monkeypatch.setattr(inbound_buffer, "mark_outbound_send_started", mark)
    monkeypatch.setattr(inbound_buffer, "record_outbound_message", record)

    await inbound_buffer.send_with_delivery_record(
        send,
        "job-1",
        "lease-1",
        pre_send_guard=guard,
        instagram_recipient_id="ig-user",
        to="ig-user",
        text="Hola",
    )

    assert lock.await_count == 2
    assert lock.await_args_list[0].args == ("instagram", "ig-user")
    assert lock.await_args_list[1].args == ("instagram", "ig-user")
    assert guard.await_count == 2
    mark.assert_awaited_once_with("job-1", "lease-1")
    send.assert_awaited_once()
    record.assert_awaited_once()
    assert events == [
        "enter", "lock", "guard", "mark", "exit",
        "enter", "lock", "guard", "send", "record", "exit",
    ]


@pytest.mark.asyncio
async def test_ambiguous_retryable_meta_failure_is_never_sent_twice():
    from app.channels.meta_errors import MetaSendError
    from app.webhooks.inbound_buffer import send_with_delivery_record

    send = AsyncMock(
        side_effect=MetaSendError(
            "server outcome unknown",
            retryable=True,
            delivery_known=False,
        )
    )
    with pytest.raises(MetaSendError):
        await send_with_delivery_record(send, "", "")

    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_recorded_backend_id_reconciles_echo_first_row(monkeypatch):
    from app.webhooks import inbound_buffer

    fetch = AsyncMock(return_value={"id": "job-1"})
    execute = AsyncMock()
    monkeypatch.setattr(inbound_buffer.db, "fetch_one", fetch)
    monkeypatch.setattr(inbound_buffer.db, "execute", execute)

    await inbound_buffer.record_outbound_message(
        "job-1",
        "lease-1",
        {"message_id": "mid-race"},
    )

    assert "RETURNING id" in fetch.await_args.args[0]
    assert "processing_lease_token" not in fetch.await_args.args[0]
    assert "meta_instagram_outbound_echoes" in execute.await_args.args[0]
    assert execute.await_args.args[1] == {"message_id": "mid-race"}
