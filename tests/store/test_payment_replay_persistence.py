from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _transactional_db():
    database = MagicMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    database.transaction.return_value = transaction
    return database


def _proof_metadata():
    return {
        "proof_hash": "a" * 64,
        "reference": "TXN-123",
        "reference_key": "b" * 64,
        "currency": "USD",
        "amount": "28.00",
        "transaction_at": datetime.now(UTC),
    }


@pytest.mark.asyncio
async def test_payment_proof_fingerprints_are_locked_and_persisted_atomically():
    from app.crm import orders

    row = {
        "id": "order-1",
        "customer_id": "customer-1",
        "total": 28,
        "payment_status": "pending",
        "customer_totals_applied": True,
    }
    fetch_one = AsyncMock(side_effect=[None, None, None, row])
    execute = AsyncMock(return_value=None)

    with (
        patch.object(orders.db, "get_db", return_value=_transactional_db()),
        patch.object(orders.db, "fetch_one", fetch_one),
        patch.object(orders.db, "execute", execute),
    ):
        result = await orders.update_order_payment_status(
            "order-1",
            "proof_received",
            note="receipt",
            proof_metadata=_proof_metadata(),
        )

    assert result["payment_status"] == "proof_received"
    assert fetch_one.await_count == 4
    update_query, update_values = execute.await_args.args
    assert "payment_proof_hash = :proof_hash" in update_query
    assert "payment_reference_key = :reference_key" in update_query
    assert update_values["proof_hash"] == "a" * 64
    assert update_values["reference_key"] == "b" * 64


@pytest.mark.asyncio
async def test_existing_fingerprint_stops_second_order_before_update():
    from app.crm import orders

    fetch_one = AsyncMock(side_effect=[None, None, {"id": "order-1"}])
    execute = AsyncMock(return_value=None)

    with (
        patch.object(orders.db, "get_db", return_value=_transactional_db()),
        patch.object(orders.db, "fetch_one", fetch_one),
        patch.object(orders.db, "execute", execute),
    ):
        result = await orders.update_order_payment_status(
            "order-2",
            "proof_received",
            proof_metadata=_proof_metadata(),
        )

    assert result == {
        "order_id": "order-2",
        "payment_status": "replay_detected",
        "existing_order_id": "order-1",
    }
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_multiple_open_orders_are_reported_as_ambiguous_without_loading_newest():
    from app.crm import orders

    with (
        patch.object(orders.db, "fetch_all", AsyncMock(return_value=[{"id": "newest"}, {"id": "older"}])),
        patch.object(orders, "get_order", AsyncMock()) as get_order,
    ):
        order, ambiguous = await orders.get_unambiguous_open_order("customer-1")

    assert order is None
    assert ambiguous is True
    get_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_open_order_is_safe_fallback_without_session():
    from app.crm import orders

    expected = {"id": "only-order"}
    with (
        patch.object(orders.db, "fetch_all", AsyncMock(return_value=[{"id": "only-order"}])),
        patch.object(orders, "get_order", AsyncMock(return_value=expected)) as get_order,
    ):
        order, ambiguous = await orders.get_unambiguous_open_order("customer-1")

    assert order == expected
    assert ambiguous is False
    get_order.assert_awaited_once_with("only-order")
