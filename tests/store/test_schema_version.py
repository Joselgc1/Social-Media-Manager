from unittest.mock import AsyncMock

import pytest
from app import db

META_LEASE_COLUMNS = [
    {"column_name": "processing_heartbeat_at"},
    {"column_name": "processing_lease_token"},
    {"column_name": "outbound_started_at"},
    {"column_name": "outbound_message_ids"},
    {"column_name": "interaction_type"},
    {"column_name": "integration_context"},
    {"column_name": "inbound_attachments"},
]
INSTAGRAM_CONVERSATION_COLUMNS = [
    {"column_name": "instagram_media_id"},
    {"column_name": "instagram_comment_id"},
    {"column_name": "instagram_parent_comment_id"},
    {"column_name": "instagram_thread_id"},
]
INSTAGRAM_ECHO_COLUMNS = [
    {"column_name": "provider_message_id"},
    {"column_name": "recipient_id"},
    {"column_name": "message_text"},
    {"column_name": "classification"},
    {"column_name": "eligible_at"},
    {"column_name": "echo_seen_at"},
    {"column_name": "reconciled_at"},
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
                INSTAGRAM_CONVERSATION_COLUMNS,
                INSTAGRAM_ECHO_COLUMNS,
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
                    {"version": 16},
                ],
                META_LEASE_COLUMNS,
                INSTAGRAM_CONVERSATION_COLUMNS,
                INSTAGRAM_ECHO_COLUMNS,
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
                    {"version": 16},
                ],
                [{"column_name": "outbound_started_at"}],
            ]
        ),
    )

    with pytest.raises(RuntimeError, match="required Meta inbound columns"):
        await db.verify_schema_version()


@pytest.mark.asyncio
async def test_store_schema_version_rejects_missing_instagram_echo_artifacts(monkeypatch):
    monkeypatch.setattr(
        db,
        "fetch_all",
        AsyncMock(
            side_effect=[
                [{"version": version} for version in range(1, db.EXPECTED_SCHEMA_VERSION + 1)],
                META_LEASE_COLUMNS,
                INSTAGRAM_CONVERSATION_COLUMNS,
                [],
            ]
        ),
    )

    with pytest.raises(RuntimeError, match="meta_instagram_outbound_echoes"):
        await db.verify_schema_version()
