from unittest.mock import AsyncMock

import pytest
from app import db


@pytest.mark.asyncio
async def test_store_schema_version_accepts_exact_supported_version(monkeypatch):
    monkeypatch.setattr(db, "fetch_one", AsyncMock(return_value={"version": db.EXPECTED_SCHEMA_VERSION}))

    await db.verify_schema_version()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, 1, 2, 4])
async def test_store_schema_version_rejects_missing_old_or_new_versions(monkeypatch, version):
    row = {"version": version} if version is not None else None
    monkeypatch.setattr(db, "fetch_one", AsyncMock(return_value=row))

    with pytest.raises(RuntimeError, match="schema version mismatch"):
        await db.verify_schema_version()


@pytest.mark.asyncio
async def test_store_schema_version_explains_unversioned_upgrade(monkeypatch):
    monkeypatch.setattr(db, "fetch_one", AsyncMock(side_effect=RuntimeError("missing table")))

    with pytest.raises(RuntimeError, match="002_existing_database_upgrade.sql"):
        await db.verify_schema_version()
