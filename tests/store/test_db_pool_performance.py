from types import SimpleNamespace

import pytest


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeDatabase:
    def __init__(self):
        self.connected = False
        self.disconnected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    def transaction(self, *args, **kwargs):
        return _FakeTransaction()

    async def fetch_one(self, *args, **kwargs):
        return None

    async def fetch_all(self, *args, **kwargs):
        return []

    async def execute(self, *args, **kwargs):
        return None

    async def execute_many(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_database_pool_uses_configured_bounds(monkeypatch):
    from app import db

    raw = _FakeDatabase()
    captured = {}

    def fake_database(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return raw

    monkeypatch.setattr(
        db,
        "get_config",
        lambda: SimpleNamespace(
            database_url="postgresql://example/test",
            database_pool_min_size=5,
            database_pool_max_size=30,
        ),
    )
    monkeypatch.setattr(db.databases, "Database", fake_database)
    db._db = None

    await db.connect()
    try:
        assert raw.connected is True
        assert captured == {
            "url": "postgresql://example/test",
            "min_size": 5,
            "max_size": 30,
        }
    finally:
        await db.disconnect()
        db._db = None

    assert raw.disconnected is True


@pytest.mark.asyncio
async def test_database_pool_rejects_invalid_bounds(monkeypatch):
    from app import db

    monkeypatch.setattr(
        db,
        "get_config",
        lambda: SimpleNamespace(
            database_url="postgresql://example/test",
            database_pool_min_size=31,
            database_pool_max_size=30,
        ),
    )
    db._db = None

    with pytest.raises(RuntimeError, match="DATABASE_POOL_MIN_SIZE"):
        await db.connect()

    db._db = None


@pytest.mark.asyncio
async def test_inbound_database_timing_collector_tracks_hot_path_stages():
    from app import db

    wrapped = db._InstrumentedDatabase(_FakeDatabase())

    with db.collect_query_timings() as timings:
        async with wrapped.transaction():
            await wrapped.fetch_one(
                query="SELECT pg_advisory_xact_lock(hashtext(:correlation_id))",
                values={"correlation_id": "chat-1"},
            )
            await wrapped.fetch_one(
                query="""
                SELECT job_id FROM kommo_message_receipts
                WHERE external_message_id = :external_message_id
                """,
                values={"external_message_id": "message-1"},
            )
            await wrapped.fetch_one(
                query="""
                SELECT id FROM kommo_message_jobs
                WHERE correlation_id = :correlation_id
                  AND status = 'pending'
                FOR UPDATE
                """,
                values={"correlation_id": "chat-1"},
            )
            await wrapped.execute(
                query="INSERT INTO kommo_message_jobs (correlation_id) VALUES (:correlation_id)",
                values={"correlation_id": "chat-1"},
            )
            await wrapped.execute(
                query="""
                INSERT INTO kommo_message_receipts (external_message_id)
                VALUES (:external_message_id)
                """,
                values={"external_message_id": "message-1"},
            )

    assert set(timings) >= {
        "transaction_acquire_ms",
        "advisory_lock_ms",
        "duplicate_lookup_ms",
        "pending_lookup_ms",
        "job_mutation_ms",
        "receipt_insert_ms",
        "transaction_commit_ms",
    }
    assert all(value >= 0 for value in timings.values())


def test_inbound_query_classifier_keeps_receipt_and_job_stages_distinct():
    from app import db

    assert db._query_timing_stage(
        "execute",
        "INSERT INTO kommo_message_receipts (external_message_id) VALUES (:id)",
    ) == "receipt_insert_ms"
    assert db._query_timing_stage(
        "execute",
        "UPDATE kommo_message_jobs SET combined_message = :message WHERE id = :id",
    ) == "job_mutation_ms"
