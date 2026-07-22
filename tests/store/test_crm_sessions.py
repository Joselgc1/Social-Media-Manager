import json
import uuid
from unittest.mock import AsyncMock

import pytest
from app.crm import sessions


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _DBHandle:
    def transaction(self):
        return _Tx()


def _row(**overrides) -> dict:
    row = {
        "customer_id": "customer-1",
        "active_agent": "legacy",
        "active_intent": None,
        "workflow_stage": "idle",
        "checkout_draft": {},
        "current_order_id": None,
        "last_route_confidence": None,
        "created_at": None,
        "updated_at": None,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_session_creation_uses_atomic_upsert(monkeypatch):
    fetch_one = AsyncMock(return_value=_row())
    monkeypatch.setattr(sessions.db, "fetch_one", fetch_one)

    session = await sessions.get_or_create_session("customer-1")

    assert session.customer_id == "customer-1"
    query = fetch_one.await_args.args[0]
    assert "INSERT INTO conversation_sessions" in query
    assert "ON CONFLICT (customer_id) DO UPDATE" in query
    assert fetch_one.await_args.args[1] == {"customer_id": "customer-1"}


@pytest.mark.asyncio
async def test_existing_session_retrieval(monkeypatch):
    fetch_one = AsyncMock(return_value=_row(active_agent="checkout", workflow_stage="checkout_collecting"))
    monkeypatch.setattr(sessions.db, "fetch_one", fetch_one)

    session = await sessions.get_session("customer-1")

    assert session.active_agent == "checkout"
    assert session.workflow_stage == "checkout_collecting"


@pytest.mark.asyncio
async def test_set_active_agent_normalizes_uuid_customer_id(monkeypatch):
    customer_id = uuid.UUID("4534aae1-e5b3-47b2-b321-8250a1e20444")
    fetch_one = AsyncMock(return_value=_row(customer_id=str(customer_id), active_agent="sales", workflow_stage="sales"))
    monkeypatch.setattr(sessions.db, "fetch_one", fetch_one)

    session = await sessions.set_active_agent(customer_id, "sales", workflow_stage="sales")

    values = fetch_one.await_args.args[1]
    assert values["customer_id"] == str(customer_id)
    assert session.customer_id == str(customer_id)
    assert session.active_agent == "sales"


@pytest.mark.asyncio
async def test_partial_draft_updates_preserve_existing_fields(monkeypatch):
    fetch_one = AsyncMock(side_effect=[
        _row(checkout_draft={"items": [{"product_query": "pijama", "size": "S"}]}),
        {"checkout_draft": {"items": [{"product_query": "pijama", "size": "S"}]}},
        _row(
            active_agent="checkout",
            workflow_stage="checkout_collecting",
            checkout_draft={"items": [{"product_query": "pijama", "size": "S"}], "shipping_method": "mrw"},
        ),
    ])
    monkeypatch.setattr(sessions.db, "fetch_one", fetch_one)
    monkeypatch.setattr(sessions.db, "get_db", lambda: _DBHandle())

    session = await sessions.update_checkout_draft("customer-1", {"shipping_method": "mrw"})

    assert "FOR UPDATE" in fetch_one.await_args_list[1].args[0]
    update_values = fetch_one.await_args_list[2].args[1]
    persisted = json.loads(update_values["checkout_draft"])

    assert persisted["items"][0]["product_query"] == "pijama"
    assert persisted["items"][0]["size"] == "S"
    assert persisted["shipping_method"] == "mrw"
    assert session.checkout_draft.shipping_method == "mrw"


@pytest.mark.asyncio
async def test_reset_behavior(monkeypatch):
    fetch_one = AsyncMock(return_value=_row(active_agent="legacy", workflow_stage="idle", checkout_draft={}))
    monkeypatch.setattr(sessions.db, "fetch_one", fetch_one)

    session = await sessions.reset_session("customer-1")

    query = fetch_one.await_args.args[0]
    assert "checkout_draft = '{}'::jsonb" in query
    assert session.active_agent == "legacy"
    assert session.workflow_stage == "idle"
    assert session.checkout_draft.public_dict() == {}


@pytest.mark.asyncio
async def test_current_order_association(monkeypatch):
    fetch_one = AsyncMock(return_value=_row(active_agent="checkout", workflow_stage="waiting_for_payment", current_order_id="order-1"))
    monkeypatch.setattr(sessions.db, "fetch_one", fetch_one)

    session = await sessions.set_current_order("customer-1", "order-1")

    values = fetch_one.await_args.args[1]
    assert values["order_id"] == "order-1"
    assert session.current_order_id == "order-1"
    assert session.workflow_stage == "waiting_for_payment"


def test_invalid_json_and_unexpected_values_are_normalized():
    session = sessions.ConversationSession.model_validate({
        "customer_id": "customer-1",
        "active_agent": "checkout; drop table",
        "workflow_stage": "weird",
        "checkout_draft": "not-json",
        "last_route_confidence": 4,
    })

    assert session.active_agent == "legacy"
    assert session.workflow_stage == "idle"
    assert session.checkout_draft.public_dict() == {}
    assert session.last_route_confidence == 1.0


def test_merge_item_patch_updates_first_item():
    draft = sessions.merge_checkout_draft(
        {"items": [{"product_query": "pijama", "quantity": 1}]},
        {"size": "m"},
    )

    assert draft.items[0].product_query == "pijama"
    assert draft.items[0].quantity == 1
    assert draft.items[0].size == "M"


def test_migration_uses_safe_deletion_behavior():
    migration = sessions.__file__.replace("store/app/crm/sessions.py", "store/migrations/001_schema.sql")
    with open(migration, encoding="utf-8") as handle:
        sql = handle.read()

    assert "REFERENCES customers(id) ON DELETE CASCADE" in sql
    assert "REFERENCES orders(id) ON DELETE SET NULL" in sql
