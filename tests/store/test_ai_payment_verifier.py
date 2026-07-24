from unittest.mock import AsyncMock

import pytest
from app.ai.payment import verifier


def _order(**overrides) -> dict:
    order = {
        "id": "order-1",
        "order_id": "order-1",
        "total": 28.0,
        "payment_method": "Zelle",
        "payment_status": "pending",
    }
    order.update(overrides)
    return order


def _payment_methods() -> list[dict]:
    return [{"id": "pm-zelle", "name": "Zelle", "information": "Correo: pagos@example.com"}]


def _vision(**overrides) -> dict:
    vision = {
        "analyzed": True,
        "payment_method": "zelle",
        "amount": "28.00",
        "status": "completed",
        "recipient_identifier": "pagos@example.com",
        "summary": "Pago Zelle a pagos@example.com por $28.00",
    }
    vision.update(overrides)
    return vision


_DEFAULT_UPDATE_RESULT = {"order_id": "order-1", "payment_status": "proof_received"}


def _patch_order(monkeypatch, order: dict | None, *, update_result=_DEFAULT_UPDATE_RESULT, session=None, session_order=None):
    get_latest_open_order = AsyncMock(return_value=order)
    get_customer_open_order_by_id = AsyncMock(return_value=session_order)
    update_order_payment_status = AsyncMock(return_value=update_result)
    get_session = AsyncMock(return_value=session)
    set_current_order = AsyncMock(return_value=None)
    monkeypatch.setattr(verifier.orders, "get_latest_open_order", get_latest_open_order)
    monkeypatch.setattr(verifier.orders, "get_customer_open_order_by_id", get_customer_open_order_by_id)
    monkeypatch.setattr(verifier.orders, "update_order_payment_status", update_order_payment_status)
    monkeypatch.setattr(verifier.sessions, "get_session", get_session)
    monkeypatch.setattr(verifier.sessions, "set_current_order", set_current_order)
    return get_latest_open_order, get_customer_open_order_by_id, update_order_payment_status, set_current_order


@pytest.mark.asyncio
async def test_valid_payment_proof_updates_order_and_payment_session(monkeypatch):
    _, _, update_order_payment_status, set_current_order = _patch_order(monkeypatch, _order())

    result = await verifier.verify_payment_proof(
        "customer-1",
        _payment_methods(),
        _vision(),
        confirmation_note="Comprobante Zelle",
    )

    assert result.status == "verified"
    assert result.order_id == "order-1"
    assert result.verified is True
    update_order_payment_status.assert_awaited_once_with(
        "order-1",
        status="proof_received",
        note="Comprobante Zelle",
    )
    set_current_order.assert_awaited_once_with(
        "customer-1",
        "order-1",
        workflow_stage="completed",
        active_agent="payment",
    )


@pytest.mark.asyncio
async def test_duplicate_payment_proof_is_idempotent(monkeypatch):
    _, _, update_order_payment_status, set_current_order = _patch_order(
        monkeypatch,
        _order(payment_status="proof_received"),
    )

    result = await verifier.verify_payment_proof("customer-1", _payment_methods(), _vision(status="pending"))

    assert result.status == "verified"
    update_order_payment_status.assert_not_awaited()
    set_current_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_payment_proof_without_open_order_does_not_update(monkeypatch):
    _, _, update_order_payment_status, set_current_order = _patch_order(monkeypatch, None)

    result = await verifier.verify_payment_proof("customer-1", _payment_methods(), _vision())

    assert result.status == "no_open_order"
    update_order_payment_status.assert_not_awaited()
    set_current_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreadable_payment_proof_does_not_update(monkeypatch):
    _, _, update_order_payment_status, _ = _patch_order(monkeypatch, _order())

    result = await verifier.verify_payment_proof("customer-1", _payment_methods(), {"analyzed": False})

    assert result.status == "unreadable"
    update_order_payment_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_amount_mismatch_does_not_update(monkeypatch):
    _, _, update_order_payment_status, _ = _patch_order(monkeypatch, _order(total=28.0))

    result = await verifier.verify_payment_proof("customer-1", _payment_methods(), _vision(amount="20.00"))

    assert result.status == "amount_mismatch"
    assert result.expected_amount is not None
    assert result.detected_amount is not None
    update_order_payment_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_method_mismatch_does_not_update(monkeypatch):
    _, _, update_order_payment_status, _ = _patch_order(monkeypatch, _order(payment_method="Zelle"))

    result = await verifier.verify_payment_proof(
        "customer-1",
        _payment_methods(),
        _vision(payment_method="binance"),
    )

    assert result.status == "method_mismatch"
    update_order_payment_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_recipient_mismatch_does_not_update(monkeypatch):
    _, _, update_order_payment_status, _ = _patch_order(monkeypatch, _order())

    result = await verifier.verify_payment_proof(
        "customer-1",
        _payment_methods(),
        _vision(recipient_identifier="otra-persona@example.com", summary="Pago Zelle a otra-persona@example.com"),
    )

    assert result.status == "recipient_mismatch"
    update_order_payment_status.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("detected_status", ["pending", "failed"])
async def test_not_completed_proof_does_not_update(monkeypatch, detected_status):
    _, _, update_order_payment_status, _ = _patch_order(monkeypatch, _order())

    result = await verifier.verify_payment_proof(
        "customer-1",
        _payment_methods(),
        _vision(status=detected_status),
    )

    assert result.status == "not_completed"
    update_order_payment_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_failure_returns_manual_review(monkeypatch):
    _, _, update_order_payment_status, set_current_order = _patch_order(monkeypatch, _order(), update_result=None)

    result = await verifier.verify_payment_proof("customer-1", _payment_methods(), _vision())

    assert result.status == "manual_review"
    update_order_payment_status.assert_awaited_once()
    set_current_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_validation_only_mode_does_not_mutate_order_or_session(monkeypatch):
    _, _, update_order_payment_status, set_current_order = _patch_order(monkeypatch, _order())

    result = await verifier.verify_payment_proof("customer-1", _payment_methods(), _vision(), update_order=False)

    assert result.status == "verified"
    update_order_payment_status.assert_not_awaited()
    set_current_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_payment_verifier_prefers_session_current_order(monkeypatch):
    session = verifier.sessions.ConversationSession(
        customer_id="customer-1",
        active_agent="checkout",
        workflow_stage="waiting_for_payment",
        current_order_id="session-order",
    )
    latest_order = _order(id="latest-order", order_id="latest-order", total=20.0)
    session_order = _order(id="session-order", order_id="session-order", total=28.0)
    get_latest_open_order, get_customer_open_order_by_id, update_order_payment_status, _ = _patch_order(
        monkeypatch,
        latest_order,
        session=session,
        session_order=session_order,
    )

    result = await verifier.verify_payment_proof("customer-1", _payment_methods(), _vision())

    assert result.status == "verified"
    get_customer_open_order_by_id.assert_awaited_once_with("customer-1", "session-order")
    get_latest_open_order.assert_not_awaited()
    update_order_payment_status.assert_awaited_once_with(
        "session-order",
        status="proof_received",
        note=None,
    )
