"""Real PostgreSQL concurrency coverage for production-critical Store invariants.

These tests intentionally exercise PostgreSQL transactions, advisory locks, row locks,
unique constraints, and SKIP LOCKED behavior. External providers and Google Sheets
remain deterministic fakes.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import databases
import pytest
from app import db
from app.integrations.kommo.models import NormalizedKommoEvent, SalesbotWidgetData

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
_MIGRATED = False


def _migration_runner():
    path = Path(__file__).resolve().parents[2] / "store/scripts/migrate.py"
    spec = importlib.util.spec_from_file_location("store_postgres_integration_migration_runner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
async def real_postgres_database():
    """Give every test a real pool bound to that test's asyncio event loop."""
    global _MIGRATED

    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")

    if not _MIGRATED:
        await _migration_runner().run_migration(TEST_DATABASE_URL)
        _MIGRATED = True

    previous_database = db._db
    database = databases.Database(TEST_DATABASE_URL, min_size=1, max_size=30)
    await database.connect()
    db._db = database
    db.invalidate_settings_cache()
    try:
        await db.execute(
            """
            TRUNCATE TABLE
                payment_proof_replays,
                kommo_message_receipts,
                kommo_outbound_deliveries,
                kommo_message_jobs,
                customer_channel_mappings,
                conversation_sessions,
                conversations,
                orders,
                customers
            RESTART IDENTITY CASCADE
            """
        )
        yield database
    finally:
        db.invalidate_settings_cache()
        db._db = previous_database
        await database.disconnect()


def _incoming_event(message_id: str, *, customer: int = 1, text: str | None = None) -> NormalizedKommoEvent:
    return NormalizedKommoEvent(
        event_type="incoming_message",
        message_id=message_id,
        lead_id=str(100_000 + customer),
        contact_id=str(200_000 + customer),
        chat_id=f"chat-{customer}",
        talk_id=f"talk-{customer}",
        text=text or f"Message {message_id}",
        message_type="text",
        origin="whatsapp",
        channel="whatsapp",
        author_id=f"author-{customer}",
        author_name=f"Customer {customer}",
        author_type="external",
    )


async def _count(query: str, values: dict | None = None) -> int:
    row = await db.fetch_one(query, values)
    return int(row["count"] if row else 0)


async def _create_customer(index: int) -> str:
    customer_id = await db.execute(
        """
        INSERT INTO customers (channel, platform_id, display_name)
        VALUES ('whatsapp', :platform_id, :display_name)
        RETURNING id
        """,
        {"platform_id": f"+580000{index:05d}", "display_name": f"Concurrency Customer {index}"},
    )
    return str(customer_id)


@pytest.mark.asyncio
async def test_same_external_message_concurrently_creates_one_logical_receipt_and_job():
    from app.integrations.kommo import jobs

    event = _incoming_event("same-message", customer=1, text="Hola")
    results = await asyncio.gather(*(jobs.record_incoming_event(event) for _ in range(20)))

    assert sum(result["status"] == "created" for result in results) == 1
    assert sum(result["status"] == "duplicate" for result in results) == 19
    assert len({result["job_id"] for result in results}) == 1
    assert await _count("SELECT COUNT(*) AS count FROM kommo_message_jobs") == 1
    assert await _count("SELECT COUNT(*) AS count FROM kommo_message_receipts") == 1


@pytest.mark.asyncio
async def test_unique_messages_for_same_correlation_merge_into_one_pending_job():
    from app.integrations.kommo import jobs

    events = [
        _incoming_event(f"same-customer-{index}", customer=2, text=f"part-{index}")
        for index in range(20)
    ]
    results = await asyncio.gather(*(jobs.record_incoming_event(event) for event in events))

    assert sum(result["status"] == "created" for result in results) == 1
    assert sum(result["status"] == "merged" for result in results) == 19
    assert len({result["job_id"] for result in results}) == 1
    assert await _count("SELECT COUNT(*) AS count FROM kommo_message_receipts") == 20

    row = await db.fetch_one(
        "SELECT combined_message, status FROM kommo_message_jobs WHERE correlation_id = :correlation_id",
        {"correlation_id": events[0].correlation_id},
    )
    assert row is not None
    assert row["status"] == "pending"
    assert set(str(row["combined_message"]).splitlines()) == {f"part-{index}" for index in range(20)}


@pytest.mark.asyncio
async def test_independent_customers_do_not_cross_merge_under_concurrency():
    from app.integrations.kommo import jobs

    events = [
        _incoming_event(f"independent-{index}", customer=100 + index, text=f"customer-{index}")
        for index in range(20)
    ]
    results = await asyncio.gather(*(jobs.record_incoming_event(event) for event in events))

    assert all(result["status"] == "created" for result in results)
    assert len({result["job_id"] for result in results}) == 20
    assert await _count("SELECT COUNT(*) AS count FROM kommo_message_jobs WHERE status = 'pending'") == 20
    assert await _count("SELECT COUNT(*) AS count FROM kommo_message_receipts") == 20
    assert await _count("SELECT COUNT(DISTINCT correlation_id) AS count FROM kommo_message_jobs") == 20


@pytest.mark.asyncio
async def test_concurrent_ready_workers_claim_each_job_at_most_once_with_distinct_leases():
    from app.integrations.kommo import jobs

    for index in range(10):
        await db.execute(
            """
            INSERT INTO kommo_message_jobs (
                correlation_id, external_message_id, lead_id, channel, combined_message,
                return_url, status, buffer_expires_at
            ) VALUES (
                :correlation_id, :external_message_id, :lead_id, 'whatsapp', :message,
                :return_url, 'ready', NOW()
            )
            """,
            {
                "correlation_id": f"claim-{index}",
                "external_message_id": f"claim-message-{index}",
                "lead_id": str(700_000 + index),
                "message": f"ready-{index}",
                "return_url": f"https://acme.kommo.com/api/v4/salesbot/{index}/continue/1",
            },
        )

    claims = await asyncio.gather(*(jobs._claim_ready_job() for _ in range(20)))
    claimed = [dict(row) for row in claims if row is not None]

    assert len(claimed) == 10
    assert len({str(row["id"]) for row in claimed}) == 10
    assert len({str(row["processing_lease_id"]) for row in claimed}) == 10
    assert all(row["status"] == "processing" for row in claimed)
    assert await _count("SELECT COUNT(*) AS count FROM kommo_message_jobs WHERE status = 'processing'") == 10
    assert await _count("SELECT COUNT(*) AS count FROM kommo_message_jobs WHERE status = 'ready'") == 0


@pytest.mark.asyncio
async def test_duplicate_salesbot_callback_race_has_one_effective_transition(monkeypatch):
    from app.integrations.kommo import jobs

    monkeypatch.setattr(
        jobs,
        "get_config",
        lambda: SimpleNamespace(meta_story_context_enabled=False, meta_instagram_context_enabled=False),
    )
    await db.execute(
        """
        INSERT INTO kommo_message_jobs (
            correlation_id, external_message_id, lead_id, contact_id, channel,
            interaction_type, combined_message, status, salesbot_launched_at, buffer_expires_at
        ) VALUES (
            'callback-race', 'callback-race-message', '900001', '900002', 'whatsapp',
            'private_message', 'Hola', 'waiting_for_salesbot', NOW() - INTERVAL '2 seconds', NOW()
        )
        """
    )
    issued_at = datetime.now(UTC).timestamp()
    data = SalesbotWidgetData(
        message="Hola",
        lead_id="900001",
        contact_id="900002",
        origin="whatsapp",
        interaction_type="private_message",
        expected_channel="whatsapp",
    )
    claims = {
        "entity_type": "leads",
        "entity_id": "900001",
        "account_id": "1",
        "client_uid": "integration-1",
        "jti": "callback-race-token",
        "iat": issued_at,
    }
    return_url = "https://acme.kommo.com/api/v4/salesbot/1/continue/2"

    results = await asyncio.gather(
        *(jobs.persist_salesbot_callback(data, return_url, claims) for _ in range(20))
    )

    assert sum(result["status"] == "ready" for result in results) == 1
    assert all(result["status"] in {"ready", "duplicate", "ignored"} for result in results)
    row = await db.fetch_one(
        "SELECT status, return_url, callback_claims FROM kommo_message_jobs WHERE correlation_id = 'callback-race'"
    )
    assert row is not None
    assert row["status"] == "ready"
    assert row["return_url"] == return_url
    assert await _count("SELECT COUNT(*) AS count FROM kommo_message_jobs WHERE correlation_id = 'callback-race'") == 1


async def _create_processing_instagram_mirror() -> tuple[str, str]:
    row = await db.fetch_one(
        """
        INSERT INTO kommo_message_jobs (
            correlation_id, external_message_id, lead_id, contact_id, channel,
            interaction_type, combined_message, talk_id, status, buffer_expires_at,
            processing_started_at, processing_lease_id
        ) VALUES (
            'instagram-mirror-race', 'instagram-mirror-message', '910001', '910002',
            'instagram', 'private_message', 'Precio?', '310001', 'processing', NOW(),
            NOW(), gen_random_uuid()
        )
        RETURNING id, processing_lease_id
        """
    )
    assert row is not None
    return str(row["id"]), str(row["processing_lease_id"])


@pytest.mark.asyncio
async def test_comment_discard_wins_before_direct_delivery_fence(monkeypatch):
    from app.integrations.kommo import delivery, jobs

    job_id, lease_id = await _create_processing_instagram_mirror()
    monkeypatch.setattr(
        jobs,
        "get_config",
        lambda: SimpleNamespace(meta_context_match_window_seconds=45),
    )
    real_fetch_all = db.fetch_all
    job_locked = asyncio.Event()
    allow_discard = asyncio.Event()

    async def pause_after_candidate_lock(query, values=None):
        rows = await real_fetch_all(query, values)
        if "FOR UPDATE OF private_job SKIP LOCKED" in query:
            job_locked.set()
            await allow_discard.wait()
        return rows

    monkeypatch.setattr(jobs.db, "fetch_all", pause_after_candidate_lock)

    async def reconcile():
        async with db.get_db().transaction():
            return await jobs._discard_recent_private_jobs_superseded_by_comment_callback(
                values={"entity_type": "leads", "entity_id": "910001"},
                normalized_message="precio?",
                comment_event_timestamp=datetime.now(UTC).timestamp(),
            )

    reconciliation = asyncio.create_task(reconcile())
    await asyncio.wait_for(job_locked.wait(), timeout=2)
    claim = asyncio.create_task(
        delivery._claim_delivery(
            job_id=job_id,
            media_type="text",
            request_fingerprint="comment-wins-fingerprint",
            attachment_metadata={"delivery_type": "text"},
            processing_lease_id=lease_id,
            require_direct_instagram_fence=True,
        )
    )
    await asyncio.sleep(0.05)
    allow_discard.set()

    assert await reconciliation == 1
    with pytest.raises(delivery.KommoDeliveryAbortedError):
        await claim
    job = await db.fetch_one(
        "SELECT status, processing_lease_id FROM kommo_message_jobs WHERE id = CAST(:id AS uuid)",
        {"id": job_id},
    )
    assert job["status"] == "discarded"
    assert job["processing_lease_id"] is None
    assert await _count(
        "SELECT COUNT(*) AS count FROM kommo_outbound_deliveries WHERE job_id = CAST(:id AS uuid)",
        {"id": job_id},
    ) == 0


@pytest.mark.asyncio
async def test_direct_delivery_fence_wins_before_comment_reconciliation(monkeypatch):
    from app.integrations.kommo import delivery, jobs

    job_id, lease_id = await _create_processing_instagram_mirror()
    monkeypatch.setattr(
        jobs,
        "get_config",
        lambda: SimpleNamespace(meta_context_match_window_seconds=45),
    )
    real_fetch_one = db.fetch_one
    fence_established = asyncio.Event()
    release_fence = asyncio.Event()

    async def pause_with_fence_held(query, values=None):
        row = await real_fetch_one(query, values)
        if "UPDATE kommo_outbound_deliveries" in query and "SET status = 'sending'" in query:
            fence_established.set()
            await release_fence.wait()
        return row

    monkeypatch.setattr(delivery.db, "fetch_one", pause_with_fence_held)
    claim = asyncio.create_task(
        delivery._claim_delivery(
            job_id=job_id,
            media_type="text",
            request_fingerprint="delivery-wins-fingerprint",
            attachment_metadata={"delivery_type": "text"},
            processing_lease_id=lease_id,
            require_direct_instagram_fence=True,
        )
    )
    await asyncio.wait_for(fence_established.wait(), timeout=2)

    async with db.get_db().transaction():
        skipped_while_locked = await jobs._discard_recent_private_jobs_superseded_by_comment_callback(
            values={"entity_type": "leads", "entity_id": "910001"},
            normalized_message="precio?",
            comment_event_timestamp=datetime.now(UTC).timestamp(),
        )
    assert skipped_while_locked == 0
    release_fence.set()
    assert (await claim).status == "sending"

    async with db.get_db().transaction():
        protected_after_commit = await jobs._discard_recent_private_jobs_superseded_by_comment_callback(
            values={"entity_type": "leads", "entity_id": "910001"},
            normalized_message="precio?",
            comment_event_timestamp=datetime.now(UTC).timestamp(),
        )
    assert protected_after_commit == 0
    job = await db.fetch_one(
        "SELECT status, processing_lease_id FROM kommo_message_jobs WHERE id = CAST(:id AS uuid)",
        {"id": job_id},
    )
    outbound = await db.fetch_one(
        "SELECT status FROM kommo_outbound_deliveries WHERE job_id = CAST(:id AS uuid)",
        {"id": job_id},
    )
    assert job["status"] == "processing"
    assert str(job["processing_lease_id"]) == lease_id
    assert outbound["status"] == "sending"


@pytest.mark.asyncio
async def test_same_payment_proof_concurrently_is_accepted_once_and_replayed_once(monkeypatch):
    from app.crm import orders

    monkeypatch.setattr(orders, "_apply_paid_customer_updates", AsyncMock())
    first_customer = await _create_customer(1)
    second_customer = await _create_customer(2)
    order_ids = []
    for customer_id in (first_customer, second_customer):
        order_id = await db.execute(
            """
            INSERT INTO orders (customer_id, items, total, payment_status, inventory_status)
            VALUES (:customer_id, '[]'::jsonb, 10, 'pending', 'reserved')
            RETURNING id
            """,
            {"customer_id": customer_id},
        )
        order_ids.append(str(order_id))

    proof = {
        "proof_hash": "proof-hash-1",
        "reference": "payment-reference-1",
        "reference_key": "payment-reference-key-1",
        "currency": "USD",
        "amount": 10,
        "transaction_at": datetime.now(UTC),
    }
    results = await asyncio.gather(
        *(orders.update_order_payment_status(order_id, "proof_received", proof_metadata=proof) for order_id in order_ids)
    )

    statuses = sorted(result["payment_status"] for result in results if result is not None)
    assert statuses == ["proof_received", "replay_detected"]
    assert await _count(
        "SELECT COUNT(*) AS count FROM orders WHERE payment_proof_hash = :proof_hash",
        {"proof_hash": proof["proof_hash"]},
    ) == 1
    assert await _count(
        "SELECT COUNT(*) AS count FROM orders WHERE payment_reference_key = :reference_key",
        {"reference_key": proof["reference_key"]},
    ) == 1


@pytest.mark.asyncio
async def test_stock_one_allows_exactly_one_concurrent_reservation(monkeypatch):
    from app.crm import orders

    customers = [await _create_customer(1000 + index) for index in range(10)]
    catalog = [{
        "sku": "RACE-SKU-M",
        "parent_sku": "RACE-SKU",
        "product_name": "Race Product",
        "size": "M",
        "price_usd": 10.0,
        "stock": 1,
        "active": True,
    }]
    monkeypatch.setattr(orders, "ensure_fresh_catalog", AsyncMock(return_value=None))
    monkeypatch.setattr(orders, "get_cached_catalog", lambda: catalog)
    monkeypatch.setattr(
        orders.db,
        "get_settings",
        AsyncMock(return_value={
            "payment_methods": [{"id": "cash", "name": "Test Pay", "information": "Test only"}],
            "order_discount_threshold_usd": 350,
            "order_discount_percent": 10,
        }),
    )

    state = {"stock": 1, "deductions": []}
    guard = threading.Lock()

    def fake_deduct_stock(items, operation_id=None):
        quantity = sum(int(item.get("quantity", 1)) for item in items)
        with guard:
            if quantity > state["stock"]:
                raise ValueError("Requested quantity is not available in the current catalog.")
            state["stock"] -= quantity
            state["deductions"].append(operation_id)

    def fake_inventory_operation_applied(operation_id, items, direction):
        del items, direction
        with guard:
            return operation_id in state["deductions"]

    monkeypatch.setattr(orders, "deduct_stock", fake_deduct_stock)
    monkeypatch.setattr(orders, "inventory_operation_applied", fake_inventory_operation_applied)

    async def attempt(customer_id: str):
        return await orders.create_order(
            customer_id=customer_id,
            items=[{"sku": "RACE-SKU-M", "product_name": "Race Product", "size": "M", "quantity": 1}],
            payment_method="Test Pay",
            shipping_city="Valencia",
            shipping_address="Test address",
            shipping_method="mrw",
        )

    results = await asyncio.gather(*(attempt(customer_id) for customer_id in customers), return_exceptions=True)
    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, Exception)]

    assert len(successes) == 1
    assert len(failures) == 9
    assert state["stock"] == 0
    assert len(state["deductions"]) == 1
    assert await _count("SELECT COUNT(*) AS count FROM orders WHERE inventory_status = 'reserved'") == 1
    assert await _count("SELECT COUNT(*) AS count FROM orders WHERE inventory_status = 'reservation_failed'") == 9


@pytest.mark.asyncio
async def test_real_database_stale_processing_recovery_transitions_safely():
    from app.integrations.kommo import jobs

    seed_rows = [
        ("stale-waiting", "waiting_for_salesbot", 0, False, True),
        ("stale-retry", "processing", 1, False, False),
        ("stale-unknown", "processing", 1, True, False),
        ("stale-failed", "processing", jobs.MAX_JOB_ATTEMPTS, False, False),
    ]
    for correlation_id, status, attempt_count, ai_started, launched in seed_rows:
        await db.execute(
            """
            INSERT INTO kommo_message_jobs (
                correlation_id, external_message_id, lead_id, channel, combined_message,
                status, attempt_count, buffer_expires_at, processing_started_at,
                ai_started_at, salesbot_launched_at
            ) VALUES (
                :correlation_id, :external_message_id, :lead_id, 'whatsapp', 'stale',
                :status, :attempt_count, NOW() - INTERVAL '10 minutes',
                CASE WHEN :is_processing THEN NOW() - INTERVAL '10 minutes' ELSE NULL END,
                CASE WHEN :ai_started THEN NOW() - INTERVAL '10 minutes' ELSE NULL END,
                CASE WHEN :launched THEN NOW() - INTERVAL '10 minutes' ELSE NULL END
            )
            """,
            {
                "correlation_id": correlation_id,
                "external_message_id": f"{correlation_id}-message",
                "lead_id": correlation_id,
                "status": status,
                "attempt_count": attempt_count,
                "is_processing": status == "processing",
                "ai_started": ai_started,
                "launched": launched,
            },
        )

    await jobs.recover_stale_jobs()
    rows = await db.fetch_all("SELECT correlation_id, status FROM kommo_message_jobs")
    statuses = {row["correlation_id"]: row["status"] for row in rows}

    assert statuses == {
        "stale-waiting": "failed",
        "stale-retry": "pending",
        "stale-unknown": "delivery_unknown",
        "stale-failed": "failed",
    }
