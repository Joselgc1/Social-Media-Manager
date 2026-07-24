from unittest.mock import AsyncMock

import pytest
from app import db


@pytest.mark.asyncio
async def test_store_schema_version_accepts_exact_supported_version(monkeypatch):
    monkeypatch.setattr(
        db,
        "fetch_all",
        AsyncMock(return_value=[{"version": version} for version in range(1, db.EXPECTED_SCHEMA_VERSION + 1)]),
    )

    await db.verify_schema_version()


@pytest.mark.asyncio
@pytest.mark.parametrize("versions", [[], [1, 2]])
async def test_store_schema_version_rejects_missing_old_or_new_versions(monkeypatch, versions):
    monkeypatch.setattr(db, "fetch_all", AsyncMock(return_value=[{"version": version} for version in versions]))

    with pytest.raises(RuntimeError, match="schema version mismatch"):
        await db.verify_schema_version()


@pytest.mark.asyncio
async def test_store_schema_version_explains_unversioned_upgrade(monkeypatch):
    monkeypatch.setattr(db, "fetch_all", AsyncMock(side_effect=RuntimeError("missing table")))

    with pytest.raises(RuntimeError, match="001_schema.sql"):
        await db.verify_schema_version()
