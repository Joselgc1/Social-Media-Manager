from unittest.mock import AsyncMock

import pytest
from app import db

META_LEASE_COLUMNS = [
    {"column_name": "processing_heartbeat_at"},
    {"column_name": "processing_lease_token"},
    {"column_name": "outbound_started_at"},
    {"column_name": "outbound_message_ids"},
]


@pytest.mark.asyncio
async def test_store_schema_version_accepts_exact_supported_version(monkeypatch):
    monkeypatch.setattr(
        db,
        "fetch_all",
        AsyncMock(
            side_effect=[
                [{"version": version} for version in range(1, db.EXPECTED_SCHEMA_VERSION + 1)],
                META_LEASE_COLUMNS,
            ]
        ),
    )

    await db.verify_schema_version()


@pytest.mark.asyncio
async def test_store_schema_version_accepts_consolidated_upgrade_version(monkeypatch):
    monkeypatch.setattr(
        db,
        "fetch_all",
        AsyncMock(
            side_effect=[
                [
                    {"version": 1},
                    {"version": 3},
                    {"version": 4},
                    {"version": 5},
                    {"version": 6},
                    {"version": 7},
                    {"version": 8},
                    {"version": 9},
                    {"version": 10},
                    {"version": 11},
                    {"version": 12},
                    {"version": 13},
                    {"version": 14},
                    {"version": 15},
                ],
                META_LEASE_COLUMNS,
            ]
        ),
    )

    await db.verify_schema_version()


@pytest.mark.asyncio
@pytest.mark.parametrize("versions", [[], [1], [2], [1, 2], [1, 3], [1, 3, 4]])
async def test_store_schema_version_rejects_missing_old_or_new_versions(monkeypatch, versions):
    monkeypatch.setattr(db, "fetch_all", AsyncMock(return_value=[{"version": version} for version in versions]))

    with pytest.raises(RuntimeError, match="schema version mismatch"):
        await db.verify_schema_version()


@pytest.mark.asyncio
async def test_store_schema_version_explains_unversioned_upgrade(monkeypatch):
    monkeypatch.setattr(db, "fetch_all", AsyncMock(side_effect=RuntimeError("missing table")))

    with pytest.raises(RuntimeError, match="001_schema.sql"):
        await db.verify_schema_version()


@pytest.mark.asyncio
async def test_store_schema_version_rejects_historical_upgrade_without_meta_lease_columns(monkeypatch):
    monkeypatch.setattr(
        db,
        "fetch_all",
        AsyncMock(
            side_effect=[
                [
                    {"version": 1},
                    {"version": 2},
                    {"version": 3},
                    {"version": 4},
                    {"version": 5},
                    {"version": 6},
                    {"version": 7},
                    {"version": 8},
                    {"version": 9},
                    {"version": 10},
                    {"version": 11},
                    {"version": 12},
                    {"version": 13},
                    {"version": 14},
                    {"version": 15},
                ],
                [{"column_name": "outbound_started_at"}],
            ]
        ),
    )

    with pytest.raises(RuntimeError, match="lease-fencing columns"):
        await db.verify_schema_version()
