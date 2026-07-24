from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_master_schema_version_accepts_exact_supported_version(monkeypatch):
    from app import db

    monkeypatch.setattr(db, "fetch_all", AsyncMock(return_value=[{"version": db.EXPECTED_SCHEMA_VERSION}]))

    await db.verify_schema_version()


@pytest.mark.asyncio
async def test_master_schema_version_accepts_consolidated_upgrade_version(monkeypatch):
    from app import db

    monkeypatch.setattr(db, "fetch_all", AsyncMock(return_value=[{"version": 1}, {"version": 2}]))

    await db.verify_schema_version()


@pytest.mark.asyncio
@pytest.mark.parametrize("versions", [[], [2], [1, 2, 3]])
async def test_master_schema_version_rejects_missing_old_or_new_versions(monkeypatch, versions):
    from app import db

    monkeypatch.setattr(db, "fetch_all", AsyncMock(return_value=[{"version": version} for version in versions]))

    with pytest.raises(RuntimeError, match="schema version mismatch"):
        await db.verify_schema_version()


@pytest.mark.asyncio
async def test_master_schema_version_explains_unversioned_upgrade(monkeypatch):
    from app import db

    monkeypatch.setattr(db, "fetch_all", AsyncMock(side_effect=RuntimeError("missing table")))

    with pytest.raises(RuntimeError, match="001_master_schema.sql"):
        await db.verify_schema_version()
